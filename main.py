# -*- coding: utf-8 -*-
import argparse
import configparser
import enum
import os
import sys
import threading
import time
import traceback
from concurrent.futures.thread import ThreadPoolExecutor  # noqa: F401  (保留导入, 兼容旧的外部引用)
from dataclasses import dataclass, field
from queue import Empty, PriorityQueue
from threading import RLock
from typing import Any

from tqdm import tqdm

from api.answer import Tiku
from api.ai_solver import (
    configure_default_solver,
    get_default_solver,
    normalize_ai_config,
    release_default_solver,
)
from api.auto_answer import AutoAnswer, normalize_answer_config
from api.base import Chaoxing, Account, StudyResult, configure_video_debug
from api.exceptions import LoginError, InputFormatError
from api.logger import configure_console_logger, logger
from api.notification import Notification
from api.live import Live
from api.live_process import LiveProcessor
from api.playback import FORCED_SPEED, normalize_speed, playback_gate
from api.runtime_budget import runtime_budget
from api.skip_registry import (
    DEFAULT_REGISTRY_FILE,
    configure_default_registry,
    get_default_registry,
    release_default_registry,
)

class ChapterResult(enum.Enum):
    SUCCESS=0,
    ERROR=1,
    NOT_OPEN=2,
    PENDING=3
    SKIPPED=4,   # 到达单次运行时长上限, 本次不处理


def log_error(func):
    def wrapper(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except BaseException as e:
            logger.error(f"Error in thread {threading.current_thread().name}: {e}")
            traceback.print_exception(type(e), e, e.__traceback__)
            raise

    return wrapper


def str_to_bool(value):
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(
        description="Samueli924/chaoxing",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    parser.add_argument("--use-cookies", action="store_true", help="使用cookies登录")

    parser.add_argument(
        "-c", "--config", type=str, default=None, help="使用配置文件运行程序"
    )
    parser.add_argument("-u", "--username", type=str, default=None, help="手机号账号")
    parser.add_argument("-p", "--password", type=str, default=None, help="登录密码")
    parser.add_argument(
        "-l", "--list", type=str, default=None, help="要学习的课程ID列表, 以 , 分隔"
    )
    parser.add_argument(
        "-s", "--speed", type=float, default=1.0,
        help="已废弃: 播放倍速固定为 1 倍速(超星时长校验严格, 倍速有漏任务点风险), 此参数仅为兼容旧命令保留"
    )
    parser.add_argument(
        "-j", "--jobs", type=int, default=1,
        help="同时处理的章节数 (默认1=顺序处理; 视频播放已全局串行, 调大只对文档/测验类任务点有加速作用)"
    )
    parser.add_argument(
        "--max-runtime", type=float, default=None, metavar="MINUTES",
        help="单次运行时长上限(分钟)。到点后不再开始新任务, 正在执行的任务跑完后退出; 不填或 0 表示不限制"
    )

    parser.add_argument(
        "-v",
        "--verbose",
        "--debug",
        action="store_true",
        help="启用调试模式, 输出DEBUG级别日志",
    )
    parser.add_argument(
        "-a", "--notopen-action", type=str, default="retry", 
        choices=["retry", "ask", "continue"],
        help="遇到关闭任务点时的行为: retry-重试, ask-询问, continue-继续"
    )

    parser.add_argument("--auto-sign", action="store_true", help="自动签到")

    # ---------------- 作业/考试 自动答题 ----------------
    parser.add_argument(
        "--auto-answer", action="store_true",
        help="启用作业 + 考试自动答题(刷完课后自动作答)"
    )
    parser.add_argument(
        "--answer-only", action="store_true",
        help="只做作业/考试, 不刷视频任务点(会自动启用自动答题)"
    )
    parser.add_argument(
        "--answer-submit", type=str, default=None,
        choices=["auto", "always", "never"],
        help="答题后如何提交: auto-覆盖率达标才提交, always-总是提交, never-只保存"
    )
    parser.add_argument(
        "--answer-order", type=str, default=None,
        choices=["before", "after", "both"],
        help="作业/考试与刷课的先后: after-刷完再做(默认), before-先做题再刷课, both-前后各扫一遍"
    )
    parser.add_argument(
        "--answer-debug", action="store_true",
        help="把抓到的作业/考试原始页面存到 answer_debug/ 目录, 用于排查解析问题"
    )
    parser.add_argument(
        "--video-debug", action="store_true",
        help="视频刷不过去时, 把卡片/状态/上报响应存到 video_debug/ 目录, 用于定位原因"
    )
    # 人脸采集视频: 纯 HTTP 永远做不过去(进度到片尾也不认), 默认直接跳过不浪费时间。
    # 如果怀疑是误判(其实是别的原因), 可以用 --no-skip-face-capture 强制照常播放对比。
    parser.add_argument(
        "--skip-face-capture", dest="skip_face_capture",
        action=argparse.BooleanOptionalAction, default=None,
        help="检测到视频开启人脸采集时直接跳过(默认开, 也读 [common] skip_face_capture); "
             "用 --no-skip-face-capture 强制照常播放以确认是否误判"
    )
    parser.add_argument(
        "--skip-registry", type=str, default=None,
        help=f"任务点跳过记录簿路径(默认 {DEFAULT_REGISTRY_FILE}), 下次运行据此直接跳过做不过去的任务点"
    )
    # 考试开关: 考试通常是「学完一定进度才解锁」的, 要不要考交给你决定。
    # 不传时跟随配置文件 [answer] exam 的值, 而默认值是「不考」。
    parser.add_argument(
        "--exam", dest="answer_exam", action=argparse.BooleanOptionalAction, default=None,
        help="本次处理考试(默认不考); 想代考加 --exam, 考试有次数限制, 自己考就维持默认"
    )

    # ---------------- DeepSeek AI 答题 ----------------
    parser.add_argument(
        "--deepseek-key", type=str, default=None,
        help="DeepSeek API Key, 填了就会用 DeepSeek 搜题答题(也可写在配置文件 [ai] 节)"
    )
    parser.add_argument(
        "--ai-model", type=str, default=None,
        help="AI 模型名, 默认 deepseek-flash"
    )
    parser.add_argument(
        "--ai-mode", type=str, default=None, choices=["fallback", "priority", "off"],
        help="AI 使用方式: fallback-题库优先AI兜底(默认), priority-AI优先, off-关闭"
    )
    parser.add_argument(
        "--ai-thinking", dest="ai_thinking", action=argparse.BooleanOptionalAction, default=None,
        help="开启深度思考: 需要推导的题更准, 但更慢更贵; 用 --no-ai-thinking 关闭(默认关)"
    )

    # 在解析之前捕获 -h 的行为
    if len(sys.argv) == 2 and sys.argv[1] in {"-h", "--help"}:
        parser.print_help()
        sys.exit(0)

    return parser.parse_args()


def load_config_from_file(config_path):
    """从配置文件加载设置"""
    config = configparser.ConfigParser()
    config.read(config_path, encoding="utf8")
    
    common_config: dict[str, Any] = {}
    tiku_config: dict[str, Any] = {}
    notification_config: dict[str, Any] = {}
    
    # 检查并读取common节
    if config.has_section("common"):
        common_config = dict(config.items("common"))
        # 处理course_list，将字符串转换为列表
        if "course_list" in common_config and common_config["course_list"]:
            common_config["course_list"] = [item.strip() for item in common_config["course_list"].split(",") if item.strip()]
        # 处理speed，将字符串转换为浮点数
        if "speed" in common_config:
            common_config["speed"] = float(common_config["speed"])
        if "jobs" in common_config:
            common_config["jobs"] = int(common_config["jobs"])
        # 单次运行时长上限(分钟), 0/留空表示不限制
        if "max_runtime_minutes" in common_config:
            try:
                common_config["max_runtime_minutes"] = float(common_config["max_runtime_minutes"] or 0)
            except (TypeError, ValueError):
                common_config["max_runtime_minutes"] = 0
        # 处理notopen_action，设置默认值为retry
        if "notopen_action" not in common_config:
            common_config["notopen_action"] = "retry"
        if "use_cookies" in common_config:
            common_config["use_cookies"] = str_to_bool(common_config["use_cookies"])
        if "username" in common_config and common_config["username"] is not None:
            common_config["username"] = common_config["username"].strip()
        if "password" in common_config and common_config["password"] is not None:
            common_config["password"] = common_config["password"].strip()
        if "verbose" in common_config:
            common_config["verbose"] = str_to_bool(common_config["verbose"])
        # 人脸采集视频默认直接跳过(纯 HTTP 做不到, 硬刷只会白烧时长)
        if "skip_face_capture" in common_config:
            common_config["skip_face_capture"] = str_to_bool(common_config["skip_face_capture"])
        if "skip_registry" in common_config:
            common_config["skip_registry"] = str_to_bool(common_config["skip_registry"])

    # 检查并读取tiku节
    if config.has_section("tiku"):
        tiku_config = dict(config.items("tiku"))
        # 处理数值类型转换
        for key in ["delay", "cover_rate"]:
            if key in tiku_config:
                tiku_config[key] = float(tiku_config[key])

    # 检查并读取notification节
    if config.has_section("notification"):
        notification_config = dict(config.items("notification"))

    # 检查并读取answer节(作业/考试自动答题)
    if config.has_section("answer"):
        common_config["answer"] = dict(config.items("answer"))
        # 作业与刷课的先后顺序, 写在 [answer] order 里
        if common_config["answer"].get("order"):
            common_config["answer_order"] = common_config["answer"]["order"].strip().lower()

    # 检查并读取ai节(DeepSeek 搜题答题)
    if config.has_section("ai"):
        common_config["ai"] = dict(config.items("ai"))

    return common_config, tiku_config, notification_config


def build_config_from_args(args):
    """从命令行参数构建配置"""
    # 作业/考试答题配置: 只有命令行显式给了才覆盖, 其余交给配置文件/默认值
    answer_config: dict[str, Any] = {}
    if getattr(args, "auto_answer", False) or getattr(args, "answer_only", False):
        answer_config["enable"] = "true"
    if getattr(args, "answer_submit", None):
        answer_config["submit_policy"] = args.answer_submit
    if getattr(args, "answer_debug", False):
        answer_config["debug"] = "true"
    # 考试开关: 只有命令行显式给了 --exam / --no-exam 才覆盖配置文件
    if getattr(args, "answer_exam", None) is not None:
        answer_config["exam"] = "true" if args.answer_exam else "false"

    # DeepSeek AI 配置
    ai_config: dict[str, Any] = {}
    if getattr(args, "deepseek_key", None):
        ai_config["api_key"] = args.deepseek_key
    if getattr(args, "ai_model", None):
        ai_config["model"] = args.ai_model
    if getattr(args, "ai_mode", None):
        ai_config["mode"] = args.ai_mode
    if getattr(args, "ai_thinking", None) is not None:
        ai_config["thinking"] = "true" if args.ai_thinking else "false"

    common_config = {
        "use_cookies": args.use_cookies,
        "username": args.username,
        "password": args.password,
        "course_list": [item.strip() for item in args.list.split(",") if item.strip()] if args.list else None,
        "speed": FORCED_SPEED,
        "jobs": args.jobs if args.jobs else 1,
        "max_runtime_minutes": getattr(args, "max_runtime", None),
        "notopen_action": args.notopen_action if args.notopen_action else "retry",
        "verbose": args.verbose,
        "answer_only": getattr(args, "answer_only", False),
        "answer": answer_config,
        "answer_order": getattr(args, "answer_order", None) or "after",
        "video_debug": getattr(args, "video_debug", False),
        "skip_face_capture": True if getattr(args, "skip_face_capture", None) is None
        else bool(args.skip_face_capture),
        "skip_registry": True,
        "skip_registry_path": getattr(args, "skip_registry", None) or DEFAULT_REGISTRY_FILE,
        "ai": ai_config,
    }
    return common_config, {}, {}


def init_config():
    """初始化配置"""
    args = parse_args()

    if args.config:
        common_config, tiku_config, notification_config = load_config_from_file(args.config)
        if args.verbose:
            common_config["verbose"] = True
        # 命令行给的运行时长上限优先级最高
        if getattr(args, "max_runtime", None) is not None:
            common_config["max_runtime_minutes"] = args.max_runtime
        # 命令行开关优先级高于配置文件
        answer_raw = dict(common_config.get("answer", {}))
        if getattr(args, "auto_answer", False) or getattr(args, "answer_only", False):
            answer_raw["enable"] = "true"
        if getattr(args, "answer_submit", None):
            answer_raw["submit_policy"] = args.answer_submit
        if getattr(args, "answer_debug", False):
            answer_raw["debug"] = "true"
        # 考试开关: --no-exam 时即使配置文件写了 exam=true 也跳过考试
        if getattr(args, "answer_exam", None) is not None:
            answer_raw["exam"] = "true" if args.answer_exam else "false"
        common_config["answer"] = answer_raw
        common_config["answer_only"] = getattr(args, "answer_only", False)
        # --answer-order 覆盖 [answer] order
        if getattr(args, "answer_order", None):
            common_config["answer_order"] = args.answer_order

        # 命令行给的 DeepSeek 参数优先级最高
        ai_raw = dict(common_config.get("ai", {}))
        if getattr(args, "deepseek_key", None):
            ai_raw["api_key"] = args.deepseek_key
        if getattr(args, "ai_model", None):
            ai_raw["model"] = args.ai_model
        if getattr(args, "ai_mode", None):
            ai_raw["mode"] = args.ai_mode
        if getattr(args, "ai_thinking", None) is not None:
            ai_raw["thinking"] = "true" if args.ai_thinking else "false"
        common_config["ai"] = ai_raw

        # 跳过记录簿与「人脸采集视频直接跳过」的开关
        common_config["skip_registry_path"] = (
            getattr(args, "skip_registry", None)
            or common_config.get("skip_registry_file")
            or DEFAULT_REGISTRY_FILE
        )
        common_config.setdefault("skip_registry", True)
        if getattr(args, "skip_face_capture", None) is not None:
            common_config["skip_face_capture"] = bool(args.skip_face_capture)
        common_config.setdefault("skip_face_capture", True)
        return common_config, tiku_config, notification_config
    else:
        return build_config_from_args(args)



def init_chaoxing(common_config, tiku_config, answer_config=None):
    """初始化超星实例"""
    username = common_config.get("username", "")
    password = common_config.get("password", "")
    use_cookies = common_config.get("use_cookies", False)
    
    # 如果没有提供用户名密码，从命令行获取
    if (not username or not password) and not use_cookies:
        username = input("请输入你的手机号, 按回车确认\n手机号:")
        password = input("请输入你的密码, 按回车确认\n密码:")
    
    account = Account(username, password)
    
    # 设置题库
    tiku = Tiku()
    tiku.config_set(tiku_config)  # 载入配置
    tiku = tiku.get_tiku_from_config()  # 载入题库
    tiku.init_tiku()  # 初始化题库
    
    # 获取查询延迟设置
    
    # 检查大模型连接（如果使用的是大模型题库）
    # 根据配置文件中的 provider 判断是否为大模型题库
    provider = tiku_config.get('provider', '')
    provider_list = [name.strip() for name in provider.split(',') if name.strip()]
    if any(name in ['AI', 'SiliconFlow', 'DeepSeek'] for name in provider_list):
        check_connection = tiku_config.get('check_llm_connection', 'true').lower() == 'true'
        if check_connection:
            logger.info(f'正在验证大模型配置 (provider={provider})...')
            if not tiku.check_llm_connection():
                logger.error('大模型连接检查失败')
                choice = input('大模型连接检查失败，无法准确答题，是否继续运行？(Y/n): ').strip().lower()
                # 直接回车默认继续运行
                if choice not in ('', 'y', 'yes'):
                    raise RuntimeError('用户取消运行')
                logger.info('用户选择继续运行...')

    query_delay = tiku_config.get("delay", 0)
    
    # 实例化超星API
    # ai_solver: 章节测验(每小节那份作业)也能用大模型作答, 不再只依赖题库
    # answer_policy: 章节测验的提交策略, 与作业/考试共用 [answer] 配置
    solver = get_default_solver()
    chaoxing = Chaoxing(
        account=account, tiku=tiku, query_delay=query_delay,
        ai_solver=solver,
        ai_mode=getattr(getattr(solver, "config", None), "mode", "fallback"),
        answer_policy=answer_config or {},
        # 人脸采集的视频直接跳过: 纯 HTTP 做不过去, 硬刷只会把时长烧在这个任务点上
        skip_face_capture=common_config.get("skip_face_capture", True),
    )
    
    return chaoxing

def process_job(chaoxing: Chaoxing, course: dict, job: dict, job_info: dict, speed: float) -> StudyResult:
    """处理单个任务点"""
    # 视频任务
    if job["type"] == "video":
        logger.trace(f"识别到视频任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        # 超星的接口没有返回当前任务是否为Audio音频任务
        video_result = chaoxing.study_video(
            course, job, job_info, _speed=speed, _type="Video"
        )
        if video_result.is_failure():
            logger.warning("当前任务非视频任务, 正在尝试音频任务解码")
            video_result = chaoxing.study_video(
                course, job, job_info, _speed=speed, _type="Audio")
        if video_result.is_failure():
            logger.warning(
                f"出现异常任务 -> 任务章节: {course['title']} 任务ID: {job['jobid']}, 已跳过"
            )
        return video_result
    # 文档任务
    elif job["type"] == "document":
        logger.trace(f"识别到文档任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        return chaoxing.study_document(course, job)
    # 测验任务
    elif job["type"] == "workid":
        logger.trace(f"识别到章节检测任务, 任务章节: {course['title']}")
        return chaoxing.study_work(course, job, job_info)
    # 阅读任务
    elif job["type"] == "read":
        logger.trace(f"识别到阅读任务, 任务章节: {course['title']}")
        return chaoxing.study_read(course, job, job_info)
    # 直播任务
    elif job["type"] == "live":
        logger.trace(f"识别到直播任务, 任务章节: {course['title']} 任务ID: {job['jobid']}")
        try:
            # 准备直播所需参数
            defaults = {
                "userid": chaoxing.get_uid(),
                "clazzId": course.get("clazzId"),
                "knowledgeid": job_info.get("knowledgeid")
            }
            
            # 创建直播对象
            live = Live(
                attachment=job,
                defaults=defaults,
                course_id=course.get("courseId")
            )
            
            # 启动直播处理线程
            thread = threading.Thread(
                target=LiveProcessor.run_live,
                args=(live, speed),
                daemon=True
            )
            thread.start()
            thread.join()  # 等待直播处理完成
            return StudyResult.SUCCESS
        except Exception as e:
            logger.error(f"处理直播任务时出错: {str(e)}")
            return StudyResult.ERROR

    logger.error(f"未知任务类型: {job['type']}")
    return StudyResult.ERROR


@dataclass(order=True)
class ChapterTask:
    index: int
    point: dict[str, Any] = field(compare=False)
    result: ChapterResult = field(default=ChapterResult.PENDING, compare=False)
    tries: int = field(default=0, compare=False)

class JobProcessor:
    STOP_POINT = {"__stop__": True}

    def __init__(self, chaoxing: Chaoxing, course: dict[str, Any], tasks: list[ChapterTask], config: dict[str, Any]):
        if "jobs" not in config or not config["jobs"]:
            config["jobs"] = 1
        
        self.chaoxing = chaoxing
        self.course = course
        # 倍速固定 1.0; 即使外部绕过了 init_config 也拉不上去
        self.speed = normalize_speed(config.get("speed", FORCED_SPEED))
        self.max_tries = 5
        self.tasks = tasks
        self.failed_tasks: list[ChapterTask] = []
        # 因为到达单次运行时长上限而没开始的任务
        self.skipped_tasks: list[ChapterTask] = []
        self.budget_stop = threading.Event()
        self.budget_monitor: threading.Thread | None = None
        self.task_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.retry_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.wait_queue: PriorityQueue[ChapterTask] = PriorityQueue()
        self.threads: list[threading.Thread] = []
        self.retry_worker: threading.Thread | None = None
        self.worker_num = config["jobs"]
        self.config = config

    def _stop_task(self) -> ChapterTask:
        return ChapterTask(index=sys.maxsize, point=self.STOP_POINT)

    @staticmethod
    def _is_stop_task(task: ChapterTask) -> bool:
        return task.point.get("__stop__") is True

    def run(self):
        for task in self.tasks:
            self.task_queue.put(task)

        for i in range(self.worker_num):
            thread = threading.Thread(target=self.worker_thread, daemon=True)
            self.threads.append(thread)
            thread.start()

        self.retry_worker = threading.Thread(target=self.retry_thread, daemon=True)
        self.retry_worker.start()

        # 时间盒: 到点后把还没开始的任务摘掉, 让下面的 join() 能返回。
        # 已在执行的任务不受影响, 会正常跑完。
        if runtime_budget.enabled:
            self.budget_monitor = threading.Thread(target=self._budget_monitor, daemon=True)
            self.budget_monitor.start()

        self.task_queue.join()

        self.budget_stop.set()
        self.retry_queue.put(self._stop_task())
        for _ in self.threads:
            self.task_queue.put(self._stop_task())

        for thread in self.threads:
            thread.join()
        if self.retry_worker:
            self.retry_worker.join()

    def _budget_monitor(self):
        """盯着运行时长, 到点就把队列里还没开始的任务全部摘掉"""
        while not self.budget_stop.is_set():
            if runtime_budget.should_stop():
                self._abort_pending()
                return
            self.budget_stop.wait(2)

    def _abort_pending(self):
        """把队列里尚未开始的任务标记为跳过

        这些任务还没被 worker 取走, 所以直接 task_done 让 join() 可以返回;
        正在执行的任务不在队列里, 会照常跑完。
        """
        skipped = 0
        while True:
            try:
                task = self.task_queue.get_nowait()
            except Empty:
                break
            if self._is_stop_task(task):
                self.task_queue.task_done()
                continue
            task.result = ChapterResult.SKIPPED
            self.skipped_tasks.append(task)
            self.task_queue.task_done()
            skipped += 1

        if skipped:
            logger.warning(
                f"[时长上限] 本课程还有 {skipped} 个章节没开始, 已跳过, 下次运行会继续处理"
            )


    @log_error
    def worker_thread(self):
        tqdm.set_lock(tqdm.get_lock())
        while True:
            task = self.task_queue.get()
            if self._is_stop_task(task):
                self.task_queue.task_done()
                logger.debug("Queue worker stopped")
                return

            # 已经到点: 这个任务还没开始, 直接跳过, 不要浪费时间
            if runtime_budget.should_stop():
                task.result = ChapterResult.SKIPPED
                self.skipped_tasks.append(task)
                self.task_queue.task_done()
                continue

            task.result = process_chapter(self.chaoxing, self.course, task.point, self.speed)

            match task.result:
                case ChapterResult.SUCCESS:
                    logger.debug("Task success: {}", task.point["title"])
                    self.task_queue.task_done()
                    logger.debug(f"unfinished task: {self.task_queue.unfinished_tasks}")

                case ChapterResult.SKIPPED:
                    # 两种来源: 到达单次运行时长上限, 或任务点卡住(片尾仍不认通过)主动放弃
                    logger.info("跳过章节(时长上限或任务点卡死): {}", task.point["title"])
                    if task not in self.skipped_tasks:
                        self.skipped_tasks.append(task)
                    self.task_queue.task_done()
                    continue

                case ChapterResult.NOT_OPEN:
                    if self.config["notopen_action"] == "continue":
                        logger.warning("章节未开启: {}, 正在跳过", task.point["title"])
                        self.task_queue.task_done()
                        continue

                    task.tries += 1
                    if task.tries >= self.max_tries:
                        logger.error(
                            "章节未开启: {} 可能由于上一章节的章节检测未完成, 也可能由于该章节因为时效已关闭，"
                            "请手动检查完成并提交再重试。或者在配置中配置(自动跳过关闭章节/开启题库并启用提交)"
                        , task.point["title"])
                        self.task_queue.task_done()
                        continue

                    # self.wait_queue.put(task)
                    self.retry_queue.put(task)

                case ChapterResult.ERROR:
                    task.tries += 1
                    logger.warning("Retrying task {} ({}/{} attempts)", task.point["title"], task.tries,
                                   self.max_tries)
                    if task.tries >= self.max_tries:
                        logger.error("Max retries reached for task: {}", task.point["title"])
                        self.failed_tasks.append(task)
                        self.task_queue.task_done()
                        continue
                    self.retry_queue.put(task)

                case _:
                    logger.error("Invalid task state {} for task {}", task.result, task.point["title"])
                    self.failed_tasks.append(task)
                    self.task_queue.task_done()

    @log_error
    def retry_thread(self):
        while True:
            task = self.retry_queue.get()
            if self._is_stop_task(task):
                self.retry_queue.task_done()
                return
            # 到点后不再安排重试, 否则 join() 会一直等下去
            if runtime_budget.should_stop():
                task.result = ChapterResult.SKIPPED
                self.skipped_tasks.append(task)
                self.retry_queue.task_done()
                continue
            self.task_queue.put(task)
            # task_done is not called when a task failed and needs to be retried so if is reinserted into the queue,
            # the task num will increase by one and become more than the real task number
            self.task_queue.task_done()
            self.retry_queue.task_done()
            time.sleep(1) # TODO: Replace with a configurable wait time


def process_chapter(chaoxing: Chaoxing, course:dict[str, Any], point:dict[str, Any], speed:float) -> ChapterResult:
    """处理单个章节"""
    # 到点就不要再发起新请求了
    if runtime_budget.should_stop():
        return ChapterResult.SKIPPED

    logger.info(f'当前章节: {point["title"]}')
    if point["has_finished"]:
        logger.info(f'章节：{point["title"]} 已完成所有任务点')
        return ChapterResult.SUCCESS
    
    # 随机等待，避免请求过快
    chaoxing.rate_limiter.limit_rate(random_time=True,random_min=0, random_max=0.2)
    
    # 获取当前章节的所有任务点
    job_info = None
    jobs, job_info = chaoxing.get_job_list(course, point)

    # 发现未开放章节, 根据配置处理
    if job_info.get("notOpen", False):
        return ChapterResult.NOT_OPEN

    # 已经默认处理空任务，此处不需要判断
    if not jobs:
        pass

    # 任务点必须按「先把内容学完, 再做本节测验/作业」的顺序走。
    # 旧实现是 ThreadPoolExecutor(5) 把整章任务点一起塞进去, 有两个问题:
    #   1. 视频在排队等播放位, 章节测验却可能先抢到作答位先跑, 表现为
    #      "视频还没看完就去答题"; 而超星的章节测验大多要求先学完本节内容;
    #   2. 章节内有多个视频时 Semaphore 不保证公平, 播放顺序是乱的。
    # 1 倍速下视频本来就是串行的, 并发并没有收益, 这里改为按页面顺序依次处理。
    job_results: list[StudyResult] = []
    content_jobs = [j for j in jobs if j.get("type") != "workid"]
    quiz_jobs = [j for j in jobs if j.get("type") == "workid"]
    ordered_jobs = content_jobs + quiz_jobs

    for idx, job in enumerate(ordered_jobs):
        if runtime_budget.should_stop():
            job_results.append(StudyResult.SKIPPED)
            continue
        if quiz_jobs and idx == len(content_jobs):
            logger.info(f"本节内容已学完, 开始做本节测验/作业: {point['title']}")
        try:
            job_results.append(process_job(chaoxing, course, job, job_info, speed))
        except Exception as exc:
            logger.error(f"任务点处理异常 -> {job.get('name')}: {type(exc).__name__}: {exc}")
            logger.error(traceback.format_exc())
            job_results.append(StudyResult.ERROR)

    for result in job_results:
        if result.is_failure():
            return ChapterResult.ERROR

    # 有任务点因为到点被跳过, 章节就不算"完成", 交给上层决定
    if any(result.is_skipped() for result in job_results):
        return ChapterResult.SKIPPED

    return ChapterResult.SUCCESS



def process_course(chaoxing: Chaoxing, course:dict[str, Any], config: dict):
    """处理单个课程"""
    logger.info(f"开始学习课程: {course['title']}")
    
    # 获取当前课程的所有章节
    point_list = chaoxing.get_course_point(
        course["courseId"], course["clazzId"], course["cpi"]
    )

    # 为了支持课程任务回滚, 采用下标方式遍历任务点

    _old_format_sizeof = tqdm.format_sizeof
    tqdm.format_sizeof = format_time
    tqdm.set_lock(RLock())

    tasks=[]

    for i, point in enumerate(point_list["points"]):
        task = ChapterTask(point=point, index=i)
        tasks.append(task)
    p = JobProcessor(chaoxing, course, tasks, config)
    p.run()

    if p.skipped_tasks:
        logger.warning(
            f"课程 {course['title']} 有 {len(p.skipped_tasks)} 个章节因到达时长上限未处理, 下次运行会接着做"
        )
    logger.info(f"课程 {course['title']} 处理结束 | {playback_gate.stats()}")

    tqdm.format_sizeof = _old_format_sizeof

def filter_courses(all_course, course_list):
    """过滤要学习的课程"""
    if not course_list:
        # 手动输入要学习的课程ID列表
        print("*" * 10 + "课程列表" + "*" * 10)
        for course in all_course:
            print(f"ID: {course['courseId']} 课程名: {course['title']}")
        print("*" * 28)
        try:
            course_list = input(
                "请输入想要学习的课程列表,以逗号分隔,例: 2151141,189191,198198\n"
            ).split(",")
        except Exception as e:
            raise InputFormatError("输入格式错误") from e
    course_list = [str(course_id).strip() for course_id in course_list if str(course_id).strip()]
    if not course_list:
        logger.warning("未指定课程ID，将学习全部课程")
        return all_course

    # 筛选需要学习的课程
    course_task = []
    course_ids = []
    for course in all_course:
        if course["courseId"] in course_list and course["courseId"] not in course_ids:
            course_task.append(course)
            course_ids.append(course["courseId"])
    
    if not course_task:
        raise InputFormatError(f"未找到指定课程ID: {', '.join(course_list)}")
    
    return course_task


def format_time(num, suffix='', divisor=''):
    total_time = round(num)
    sec = total_time % 60
    mins = (total_time % 3600) // 60
    hrs = total_time // 3600

    if hrs > 0:
        return f"{hrs:02d}:{mins:02d}:{sec:02d}"

    return f"{mins:02d}:{sec:02d}"


def write_step_summary(markdown: str) -> None:
    """把关键结论写进 GitHub Actions 的 Step Summary

    只在 Actions 里生效(有 GITHUB_STEP_SUMMARY 环境变量时)。好处是运行结束后
    在 Actions 页面上直接能看到「哪些任务点跳过了、为什么」, 不用翻几千行日志 ——
    Actions 会被中断重开, 日志看不完的话, 人事后就记不住这个任务点是怎么回事。
    """
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path or not markdown.strip():
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown if markdown.endswith("\n") else markdown + "\n")
    except OSError as exc:
        logger.debug("写 Step Summary 失败: {}", exc)


def build_step_summary(*, done_msg: str, manual_lines: list[str], inline_summary: str,
                       answer_summary: str, registry=None) -> str:
    """拼一段 markdown 摘要(Step Summary 用)"""
    head = (done_msg.splitlines() or ["本次运行结束"])[0]
    out = ["## 刷课结果", "", head, ""]

    if manual_lines:
        out += [f"### 未完成 / 已跳过 {len(manual_lines)} 个任务点", "",
                "| 任务点 | 说明 |", "| --- | --- |"]
        for line in manual_lines:
            text = line.strip().lstrip("- ").strip()
            name, _, reason = text.partition(":")
            out.append(f"| {name.strip()} | {reason.strip().replace('|', '/')} |")
        out += ["",
                "> 人脸采集的任务点必须本人在网页端/App 里过人脸, 程序不会再重试;"
                " 记录写在 `skip_registry.json`, 下次运行直接跳过。", ""]

    if inline_summary:
        out += ["### 视频弹窗题", "", inline_summary, ""]
    if answer_summary:
        out += ["### 答题情况", "", "```", answer_summary, "```", ""]
    if registry is not None:
        stats = registry.stats()
        if stats.get("permanent"):
            out += [f"### 已确认做不过去的任务点(共 {stats['permanent']} 个, 跨运行有效)", ""]
            for line in registry.summary_lines(permanent_only=True)[:20]:
                out.append(f"- {line}")
            out.append("")
    return "\n".join(out)


def main():
    """主程序入口"""
    try:
        # 初始化配置
        common_config, tiku_config, notification_config = init_config()
        configure_console_logger("DEBUG" if common_config.get("verbose", False) else "INFO")
        
        # 播放节奏: 倍速固定 1 倍速, 视频全局串行; 配置里写别的值也一律拉回
        common_config["speed"] = normalize_speed(common_config.get("speed", 1.0))
        jobs = common_config.get("jobs", 1)
        try:
            jobs = int(jobs)
        except (TypeError, ValueError):
            jobs = 1
        common_config["jobs"] = max(1, jobs)
        common_config["notopen_action"] = common_config.get("notopen_action", "retry")
        logger.info(
            f"播放节奏: {FORCED_SPEED:g} 倍速, 视频串行播放, 章节并发数 {common_config['jobs']}"
        )

        # 时间盒: 到点后不再开新任务, 正在执行的任务跑完后退出
        runtime_budget.configure(common_config.get("max_runtime_minutes"))

        # 视频诊断: 卡住时把卡片/状态/上报响应落盘, 用于区分「弹窗题 / 人脸采集 / 风控」
        configure_video_debug("video_debug" if common_config.get("video_debug") else None)

        # 跳过记录簿: 记录「哪个任务点为什么过不去」。
        # permanent 条目(如人脸采集)下次运行直接跳过, 不再白播一遍视频。
        registry_path = common_config.get("skip_registry_path") or DEFAULT_REGISTRY_FILE
        registry = configure_default_registry(
            registry_path, enabled=bool(common_config.get("skip_registry", True))
        )
        if registry is not None:
            logger.info(
                "[跳过记录] 使用 {}: 已有 {} 条记录, 其中 {} 条不会再重试",
                registry.path, registry.stats()["total"], registry.stats()["permanent"],
            )

        # 初始化 DeepSeek(没填 api_key 会自动关闭, 不影响其余功能)
        ai_config = normalize_ai_config(common_config.get("ai"))
        ai_solver = configure_default_solver(common_config.get("ai"))
        if ai_config.enabled:
            if ai_config.check_connection:
                ok, message = ai_solver.check_connection()
                if ok:
                    logger.info(f"[AI] {message}")
                else:
                    logger.error(f"[AI] 连接检查失败: {message}")
                    logger.error("[AI] 本次不使用 AI 作答, 题目将由题库/随机答案兜底")
                    ai_config.mode = "off"
                    ai_solver.config.mode = "off"
        else:
            logger.info("[AI] 未配置 DeepSeek API Key, AI 搜题答题未启用")
        
        # 作业/考试/章节测验共用同一份答题配置(题库没配好时也能跑, 只是覆盖率会低)
        answer_config = normalize_answer_config(
            common_config.get("answer"), tiku_config.get("cover_rate", 0.9)
        )

        # 初始化超星实例
        chaoxing = init_chaoxing(common_config, tiku_config, answer_config)
        
        # 设置外部通知
        notification = Notification()
        notification.config_set(notification_config)
        notification = notification.get_notification_from_config()
        notification.init_notification()
        
        # 检查当前登录状态
        _login_state = chaoxing.login(login_with_cookies=common_config.get("use_cookies", False))
        if not _login_state["status"]:
            raise LoginError(_login_state["msg"])
        
        # 获取所有的课程列表
        all_course = chaoxing.get_course_list()
        
        # 过滤要学习的课程
        course_task = filter_courses(all_course, common_config.get("course_list"))

        # 作业/考试自动答题配置(题库没配好时也能跑, 只是覆盖率会低)
        answer_only = bool(common_config.get("answer_only", False))
        if answer_config["enable"]:
            logger.info(
                f"自动答题: 作业 {'处理' if answer_config['work'] else '跳过'} / "
                f"考试 {'处理' if answer_config['exam'] else '跳过'} "
                f"(考试默认不代考, 想代考用 --exam 或配置 [answer] exam=true)"
            )
        else:
            logger.info("自动答题: 未启用 (需要 --auto-answer / --answer-only 或 [answer] enable=true)")

        # ---- 作业/考试答题器 ----
        auto_answer = AutoAnswer(chaoxing, answer_config, ai_solver=ai_solver) if answer_config["enable"] else None
        stats_list = []

        def answer_course(course: dict, stage: str) -> None:
            """对单门课程扫一遍作业/考试"""
            if auto_answer is None:
                return
            if runtime_budget.should_stop():
                logger.warning("[自动答题] 已达到单次运行时长上限, 不再处理剩余课程")
                return
            logger.info(f"[自动答题] {stage}扫描: {course.get('title')}")
            try:
                stats_list.append(auto_answer.run_course(course))
            except Exception as exc:
                logger.error(f"[自动答题] 课程 {course.get('title')} 处理异常: {type(exc).__name__}: {exc}")
                logger.error(traceback.format_exc())

        # 作业和刷课的先后顺序:
        #   after  - 先刷视频/文档, 再做这门课的作业/考试(默认)
        #   before - 先做题再刷课
        #   both   - 前后各扫一遍
        # 默认 after 的原因: 作业和考试基本都是「看完前面几节才解锁」,
        # 先做会撞上"未开始/未解锁"而白跑。注意这里是**每门课内部**的顺序,
        # 不是所有课刷完才统一做题 —— 第 1 门课的作业做完才进第 2 门课,
        # 所以即使中途撞上时长上限, 前面几门课也是"视频+作业"都完整的。
        answer_order = str(common_config.get("answer_order") or "after").lower()

        # 开始学习
        if answer_only:
            logger.info("已启用 --answer-only, 本次跳过视频/文档等任务点, 只做作业和考试")
            for course in course_task:
                answer_course(course, "唯一一轮")
        else:
            logger.info(f"课程列表过滤完毕, 当前课程任务数量: {len(course_task)}")
            for course in course_task:
                if runtime_budget.should_stop():
                    logger.warning("已达到单次运行时长上限, 不再开始新的课程")
                    break
                if answer_order in ("before", "both"):
                    answer_course(course, "刷课前")
                process_course(chaoxing, course, common_config)
                if answer_order in ("after", "both"):
                    answer_course(course, "刷课后")
            logger.info("所有课程学习任务已完成")

        # ---- 汇总 ----
        # 本次没能完成的任务点: 卡住的(进度到片尾不通过) + 主动跳过的(人脸采集等)。
        # 两者会有重叠, 按 (任务点, 原因) 去重后再打印, 免得同一件事报两遍。
        stuck_points = getattr(chaoxing, "stuck_points", None) or []
        skipped_points = getattr(chaoxing, "skipped_points", None) or []
        manual_lines: list[str] = []
        manual_seen: set[tuple] = set()

        def _add_manual(name, reason, tag: str = "") -> None:
            key = (name, reason)
            if key in manual_seen:
                return
            manual_seen.add(key)
            line = f"  - {name}: {reason}"
            if tag:
                line += f" ({tag})"
            manual_lines.append(line)

        for p in stuck_points:
            tag = ""
            if p.get("has_face_capture"):
                tag = "需本人过人脸, 程序不再重试"
            elif p.get("question_count"):
                tag = f"视频内有 {p['question_count']} 道弹窗题"
            _add_manual(p.get("name"), p.get("reason"), tag)

        skipped_summary = ""
        if skipped_points:
            for p in skipped_points:
                tag = "已记入跳过记录, 下次不再重试" if p.get("permanent") else "下次运行会重试"
                _add_manual(p.get("name"), p.get("reason"), tag)
            skipped_summary = "\n".join(
                f"  - {p.get('name')}: {p.get('reason')}"
                for p in skipped_points
            )
            logger.warning(
                "本次主动跳过 {} 个任务点(原因已记录, 能做的部分已继续):\n{}",
                len({(p.get("name"), p.get("reason")) for p in skipped_points}), skipped_summary,
            )

        manual_summary = "\n".join(manual_lines)
        if manual_lines:
            logger.warning(
                f"共 {len(manual_lines)} 个任务点本次未完成, 需要人工处理:\n{manual_summary}"
            )
            if common_config.get("video_debug"):
                logger.warning("诊断数据已存到 video_debug/ 目录, 可据此确认具体原因")
        elif not skipped_points:
            logger.info("本次没有卡住或被跳过的任务点")

        # 视频弹窗题(视频中途弹出的小题)的作答/提交情况
        inline_summary = ""
        inline_stat = getattr(chaoxing, "inline_quiz_stat", None) or {}
        if inline_stat.get("total"):
            inline_summary = (
                f"视频弹窗题: 共 {inline_stat['total']} 道, "
                f"作答 {inline_stat['answered']} 道, "
                f"提交成功 {inline_stat['submitted']} 道"
            )
            if inline_stat.get("failed"):
                inline_summary += f", 失败 {inline_stat['failed']} 道"
            logger.info(inline_summary)

        answer_summary = ""
        if stats_list:
            answer_summary = "\n".join(stats.summary() for stats in stats_list)
            logger.info("自动答题汇总:\n" + answer_summary)
            # AI 用量统计, 方便估算花了多少钱
            solver = get_default_solver()
            if solver and solver.available and solver.requests:
                logger.info(f"[AI] 本次用量: {solver.stats()}")
        elif answer_config["enable"]:
            logger.info("自动答题已启用, 但本次没有扫描到待处理的作业/考试")
        else:
            logger.info("未启用自动答题, 如需作答作业/考试请加 --auto-answer 或配置 [answer] enable=true")

        if runtime_budget.expired:
            done_msg = "chaoxing : 已达到单次运行时长上限, 本次收尾退出(下次运行会从断点继续)"
        else:
            done_msg = "chaoxing : 所有课程学习任务已完成"
        if answer_summary:
            done_msg += "\n" + answer_summary
        if inline_summary:
            done_msg += "\n" + inline_summary
        if manual_lines:
            done_msg += "\n本次未完成 / 已跳过的任务点:\n" + manual_summary
        done_msg += "\n" + runtime_budget.summary()
        logger.info(runtime_budget.summary())
        logger.info(done_msg)
        notification.send(done_msg)

        # Actions 运行结束后, 在页面上一眼能看到的关键结论(日志太长没人翻)
        write_step_summary(
            build_step_summary(
                done_msg=done_msg,
                manual_lines=manual_lines,
                inline_summary=inline_summary,
                answer_summary=answer_summary,
                registry=get_default_registry(),
            )
        )

    except SystemExit as e:
        if e.code != 0:
            logger.error(f"错误: 程序异常退出, 返回码: {e.code}")
        sys.exit(e.code)
    except KeyboardInterrupt as e:
        logger.error(f"错误: 程序被用户手动中断, {e}")
    except BaseException as e:
        logger.error(f"错误: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        try:
            notification.send(f"chaoxing : 出现错误 {type(e).__name__}: {e}\n{traceback.format_exc()}")
        except Exception:
            pass  # 如果通知发送失败，忽略异常
        raise e
    finally:
        # 把 AI 答案缓存落盘, 下次同样的题目直接命中, 不用再花钱
        release_default_solver()
        # 跳过记录簿落盘: 下次运行据此直接跳过做不过去的任务点
        release_default_registry()


if __name__ == "__main__":
    main()
