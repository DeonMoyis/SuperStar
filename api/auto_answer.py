# -*- coding: utf-8 -*-
"""
作业 / 考试 自动答题模块

仓库原本只处理「章节任务点」里的测验 (base.Chaoxing.study_work),
作业列表和考试列表是完全没被覆盖的。这个模块补上这一块:

    课程 -> 作业列表 / 考试列表 -> 未完成的条目 -> 抓题 -> 搜题 -> 填答 -> 按覆盖率提交

设计说明 (为什么这么写):
1. 超星的作业/考试页面结构经常改版, 所以列表解析不写死 CSS 选择器,
   而是从页面里所有链接/onclick 中按关键字捞出「能进入答题页的地址」,
   再把地址里的 query 参数原样保留 —— 页面给什么参数我们就用什么参数,
   这样 enc / answerId / cpi 这些奇奇怪怪的字段不用猜。
2. 提交地址优先用答题页 <form action="...">, 页面自己写的就是最准的,
   拿不到才退回已知的固定接口。
3. 解析不到任何东西时, 打开 debug 开关会把原始 HTML 存到 answer_debug/ 目录,
   方便直接对着真实页面修解析, 不用连账号反复试探。
"""
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from api.answer_engine import AnswerEngine, AnswerReport, build_submit_payload
from api.base import Chaoxing, SessionManager
from api.decode import decode_questions_info
from api.logger import logger

__all__ = ["AnswerTask", "AnswerStats", "AutoAnswer", "normalize_answer_config"]

# ---------------------------------------------------------------- 常量配置

# 作业列表页候选地址, 从新到旧依次尝试
WORK_LIST_URLS = [
    "https://mooc1.chaoxing.com/mooc2/work/list?courseId={courseId}&classId={clazzId}&cpi={cpi}&ut=s",
    "https://mooc1.chaoxing.com/mooc-ans/work/list?courseId={courseId}&classId={clazzId}&cpi={cpi}&ut=s",
]

# 考试列表页候选地址
EXAM_LIST_URLS = [
    "https://mooc1.chaoxing.com/mooc2/exam/exam-list?classId={clazzId}&courseId={courseId}&ut=s",
    "https://mooc1.chaoxing.com/mooc-ans/exam-ans/exam/test/exam-list?classId={clazzId}&courseId={courseId}&ut=s",
]

# 进入答题页的地址特征 (小写匹配)
TASK_URL_MARKERS = {
    "work": ("dohomeworknew", "workid=", "work/list/work"),
    "exam": ("reversionteststartnew", "examid=", "exam-ans/exam/test"),
}

# 提交接口兜底地址 (页面 <form action> 拿不到时才会用到)
FALLBACK_SUBMIT_URLS = {
    "work": ["https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew"],
    "exam": [
        "https://mooc1.chaoxing.com/mooc-ans/exam/test/addExamTranExamNew",
        "https://mooc1.chaoxing.com/mooc-ans/exam/test/saveExamTranExamNew",
    ],
}

# 已交/已完成的关键字: 命中就不再作答
FINISHED_KEYWORDS = (
    "已完成", "已提交", "已交", "已批阅", "待批阅", "已评分", "已通过",
    "已结束", "已过期", "已作废", "已互评",
)
# 未完成的关键字: 先判断这个, 因为「未完成」里也含「完成」两个字
UNFINISHED_KEYWORDS = (
    "未交", "未提交", "未开始", "未完成", "未参与", "待做", "待完成", "待提交", "进行中",
)

DEFAULT_ANSWER_CONFIG = {
    "enable": False,        # 是否启用自动答题
    "work": True,           # 是否处理作业
    "exam": True,           # 是否处理考试
    "submit_policy": "auto",  # auto-按覆盖率 / always-总是提交 / never-只保存
    "min_cover_rate": 0.9,  # auto 模式下的最低覆盖率
    "max_tasks": 20,        # 每门课最多处理多少份, 防止跑飞
    "debug": False,         # 是否把原始 HTML 存盘
    "debug_dir": "answer_debug",
}


def normalize_answer_config(raw: Optional[dict[str, Any]], tiku_cover_rate: float = 0.9) -> dict[str, Any]:
    """把配置文件里的字符串统一成正确的类型, 缺省值兜底"""
    config = dict(DEFAULT_ANSWER_CONFIG)
    if not raw:
        return config

    def as_bool(value, default):
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    config["enable"] = as_bool(raw.get("enable"), config["enable"])
    config["work"] = as_bool(raw.get("work"), config["work"])
    config["exam"] = as_bool(raw.get("exam"), config["exam"])
    config["debug"] = as_bool(raw.get("debug"), config["debug"])

    policy = str(raw.get("submit_policy", config["submit_policy"]) or "").strip().lower()
    if policy not in {"auto", "always", "never"}:
        logger.warning(f"submit_policy={policy!r} 无效, 已回退为 auto")
        policy = "auto"
    config["submit_policy"] = policy

    try:
        config["min_cover_rate"] = float(raw.get("min_cover_rate", tiku_cover_rate))
    except (TypeError, ValueError):
        config["min_cover_rate"] = tiku_cover_rate
    try:
        config["max_tasks"] = int(raw.get("max_tasks", config["max_tasks"]))
    except (TypeError, ValueError):
        config["max_tasks"] = DEFAULT_ANSWER_CONFIG["max_tasks"]

    config["debug_dir"] = str(raw.get("debug_dir") or config["debug_dir"]).strip()
    return config


# ---------------------------------------------------------------- 数据结构

@dataclass
class AnswerTask:
    """列表页里扫描出来的一份作业/考试"""
    kind: str            # work / exam
    title: str
    url: str
    status: str = ""
    finished: bool = False


@dataclass
class AnswerStats:
    """一门课的答题汇总"""
    course_title: str = ""
    tasks_found: int = 0
    answered: int = 0
    submitted: int = 0
    saved: int = 0
    skipped: int = 0
    failed: int = 0
    reports: list[AnswerReport] = field(default_factory=list)

    @property
    def avg_cover_rate(self) -> float:
        if not self.reports:
            return 0.0
        return sum(report.cover_rate for report in self.reports) / len(self.reports)

    def summary(self) -> str:
        return (
            f"{self.course_title or '本课程'}: 发现 {self.tasks_found} 份, "
            f"作答 {self.answered} 份 (已提交 {self.submitted} / 仅保存 {self.saved}), "
            f"跳过 {self.skipped} 份, 失败 {self.failed} 份, "
            f"平均覆盖率 {self.avg_cover_rate * 100:.0f}%"
        )


# ---------------------------------------------------------------- 列表解析

def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _extract_url_from_node(node) -> Optional[str]:
    """从一个节点上把「可能要跳转的地址」捞出来, 兼容 href 和 onclick 两种写法"""
    href = node.get("href")
    if href and not href.lower().startswith(("javascript", "#")):
        return href

    onclick = node.get("onclick") or node.get("data-url") or ""
    if onclick:
        # window.open('/mooc-ans/work/doHomeWorkNew?...') / location.href="..."
        matched = re.findall(r"['\"]([^'\"]+)['\"]", onclick)
        for candidate in matched:
            if "/" in candidate:
                return candidate
    return None


def _block_text(node) -> str:
    """向上找到「整条记录」的容器, 返回它的文本, 用来判断标题和状态"""
    parent = node
    for _ in range(5):
        if parent.parent is None:
            break
        parent = parent.parent
        if parent.name in {"li", "tr"}:
            break
        classes = " ".join(parent.get("class") or [])
        if any(word in classes.lower() for word in ("item", "work", "exam", "list")):
            break
    return _clean_text(parent.get_text(" "))


def _judge_finished(text: str) -> tuple[bool, str]:
    """根据记录文本判断是否已经完成, 同时把状态词摘出来做日志"""
    lowered = text or ""
    for keyword in UNFINISHED_KEYWORDS:
        if keyword in lowered:
            return False, keyword
    for keyword in FINISHED_KEYWORDS:
        if keyword in lowered:
            return True, keyword
    return False, ""


def decode_task_list(html_text: str, kind: str, base_url: str = "https://mooc1.chaoxing.com") -> list[AnswerTask]:
    """
    从作业/考试列表页解析出所有条目。

    不依赖具体 CSS class, 只看「链接地址里有没有能进入答题页的特征」,
    所以列表页改版时只要能点进去, 这里就还能解析出来。
    """
    soup = BeautifulSoup(html_text, "lxml")
    markers = TASK_URL_MARKERS.get(kind, ())
    tasks: list[AnswerTask] = []
    seen: set[str] = set()

    for node in soup.find_all(["a", "div", "li", "span"]):
        raw_url = _extract_url_from_node(node)
        if not raw_url:
            continue
        lowered = raw_url.lower()
        if not any(marker in lowered for marker in markers):
            continue

        url = urljoin(base_url, raw_url)
        # 同一个地址只收一次 (一个条目里往往有 href 和 onclick 两份)
        key = urlparse(url).path + "?" + urlparse(url).query
        if key in seen:
            continue
        seen.add(key)

        block = _block_text(node)
        title = _clean_text(node.get_text(" ")) or block[:80]
        finished, status = _judge_finished(block)
        tasks.append(
            AnswerTask(
                kind=kind,
                title=title[:80],
                url=url,
                status=status,
                finished=finished,
            )
        )

    return tasks


# ---------------------------------------------------------------- 主流程

class AutoAnswer:
    """作业 / 考试 自动答题器"""

    def __init__(self, chaoxing: Chaoxing, config: Optional[dict[str, Any]] = None):
        self.chaoxing = chaoxing
        self.config = normalize_answer_config(config)
        self._dumped: set[str] = set()
        query_delay = self.chaoxing.kwargs.get("query_delay", 0)
        self.engine = AnswerEngine(self.chaoxing.tiku, query_delay=query_delay)

    # ------------------------------------------------------------ 对外入口
    def run_courses(self, courses: list[dict[str, Any]]) -> list[AnswerStats]:
        all_stats = []
        for course in courses:
            all_stats.append(self.run_course(course))
        return all_stats

    def run_course(self, course: dict[str, Any]) -> AnswerStats:
        stats = AnswerStats(course_title=course.get("title", ""))
        logger.info(f"[自动答题] 开始处理课程: {course.get('title', course.get('courseId'))}")

        tasks: list[AnswerTask] = []
        if self.config["work"]:
            tasks += self._collect_tasks(course, "work")
        if self.config["exam"]:
            tasks += self._collect_tasks(course, "exam")

        stats.tasks_found = len(tasks)
        if not tasks:
            logger.info("[自动答题] 没有扫描到待完成的作业/考试")
            return stats

        pending = [task for task in tasks if not task.finished][: self.config["max_tasks"]]
        stats.skipped = len(tasks) - len(pending)
        if len(tasks) > self.config["max_tasks"]:
            logger.warning(
                f"[自动答题] 待处理条目超过上限 {self.config['max_tasks']}, "
                f"本次只处理前 {len(pending)} 份"
            )

        for index, task in enumerate(pending, start=1):
            label = "作业" if task.kind == "work" else "考试"
            logger.info(f"[自动答题] ({index}/{len(pending)}) {label}: {task.title}")
            try:
                report = self._answer_task(task)
            except Exception as exc:
                stats.failed += 1
                logger.error(f"[自动答题] {label} {task.title} 处理失败: {type(exc).__name__}: {exc}")
                continue

            if report is None:
                stats.skipped += 1
                continue

            stats.answered += 1
            stats.reports.append(report)
            if report.submitted:
                stats.submitted += 1
            else:
                stats.saved += 1

        logger.info(f"[自动答题] {stats.summary()}")
        return stats

    # ------------------------------------------------------------ 列表抓取
    def _collect_tasks(self, course: dict[str, Any], kind: str) -> list[AnswerTask]:
        templates = WORK_LIST_URLS if kind == "work" else EXAM_LIST_URLS
        session = SessionManager.get_session()

        for template in templates:
            url = template.format(
                courseId=course.get("courseId", ""),
                clazzId=course.get("clazzId", ""),
                cpi=course.get("cpi", ""),
            )
            self._limit_rate()
            try:
                resp = session.get(url, allow_redirects=True)
            except Exception as exc:
                logger.warning(f"[自动答题] 读取{kind}列表失败: {exc}")
                continue

            if resp.status_code != 200:
                logger.debug(f"[自动答题] 列表返回 {resp.status_code}: {url}")
                continue

            tasks = decode_task_list(resp.text, kind)
            if tasks:
                label = "作业" if kind == "work" else "考试"
                logger.info(f"[自动答题] {label}列表读取成功, 共 {len(tasks)} 条")
                return tasks

            logger.debug(f"[自动答题] 该地址没有解析到{kind}条目, 尝试下一个: {url}")
            self._dump(resp.text, f"list_{kind}_{course.get('courseId', '')}", when_no_task=True)

        return []

    # ------------------------------------------------------------ 单份作答
    def _answer_task(self, task: AnswerTask) -> Optional[AnswerReport]:
        session = SessionManager.get_session()
        self._limit_rate()

        html = self._fetch_page(session, task.url)
        if not html:
            return None

        # 考试可能有「考试须知/开始考试」中间页, 需要再点一次才出题
        html = self._enter_exam_if_needed(session, html)
        if not html:
            return None

        questions = self._decode_questions(html)
        if not questions:
            self._dump(html, f"{task.kind}_{task.title}")
            logger.warning(f"[自动答题] 没能从页面解析出题目, 已跳过: {task.title}")
            return None

        report = self.engine.solve_all(questions.get("questions", []), title=task.title)

        submit = self._should_submit(report)
        label = "作业" if task.kind == "work" else "考试"
        if submit:
            questions["pyFlag"] = ""
            logger.info(f"[自动答题] 覆盖率 {report.cover_rate * 100:.0f}% 达标, 提交{label}")
        else:
            questions["pyFlag"] = "1"
            logger.warning(
                f"[自动答题] 覆盖率 {report.cover_rate * 100:.0f}% "
                f"低于阈值 {self.config['min_cover_rate'] * 100:.0f}%, 只保存不提交"
            )

        payload = build_submit_payload(questions, report)
        ok = self._submit(session, task, html, payload, submit=submit)
        report.submitted = bool(ok and submit)
        report.saved = bool(ok and not submit)
        report.submitted_ok = ok
        if not ok:
            logger.error(f"[自动答题] {label}提交返回异常, 请到网页端手动确认: {task.title}")
        return report

    def _fetch_page(self, session, url: str) -> Optional[str]:
        self._limit_rate()
        try:
            resp = session.get(url, allow_redirects=True)
        except Exception as exc:
            logger.error(f"[自动答题] 打开答题页失败: {exc}")
            return None
        if resp.status_code != 200:
            logger.error(f"[自动答题] 答题页返回 {resp.status_code}: {url}")
            return None
        if "passport2.chaoxing.com" in resp.url or "请先登录" in resp.text:
            logger.error("[自动答题] 登录状态失效, 请重新运行以刷新 cookies")
            return None
        return resp.text

    def _enter_exam_if_needed(self, session, html: str) -> Optional[str]:
        """考试的第一步往往是「考试须知」页, 这里自动点进去"""
        if self._looks_like_question_page(html):
            return html

        soup = BeautifulSoup(html, "lxml")
        for node in soup.find_all(["a", "button", "div", "span"]):
            text = _clean_text(node.get_text(" "))
            if not any(word in text for word in ("开始考试", "进入考试", "继续考试", "开始答题")):
                continue
            raw_url = _extract_url_from_node(node)
            if not raw_url:
                continue
            target = urljoin("https://mooc1.chaoxing.com", raw_url)
            logger.info(f"[自动答题] 检测到考试入口页, 正在进入: {target[:80]}")
            return self._fetch_page(session, target)
        return html

    @staticmethod
    def _looks_like_question_page(html: str) -> bool:
        return "singleQuesId" in html or "TiMu" in html

    def _decode_questions(self, html: str) -> Optional[dict[str, Any]]:
        if "<form" not in html.lower():
            return None
        try:
            questions = decode_questions_info(html)
        except Exception as exc:
            logger.debug(f"[自动答题] 题目解析异常: {exc}")
            return None
        if not questions.get("questions"):
            return None
        return questions

    # ------------------------------------------------------------ 提交
    def _should_submit(self, report: AnswerReport) -> bool:
        policy = self.config["submit_policy"]
        if policy == "always":
            return True
        if policy == "never":
            return False
        if report.total == 0:
            return False
        return report.cover_rate >= self.config["min_cover_rate"]

    def _submit(self, session, task: AnswerTask, html: str, payload: dict[str, Any], submit: bool) -> bool:
        action = self._form_action(html, task.url)
        candidates = [action] if action else []
        candidates += [url for url in FALLBACK_SUBMIT_URLS.get(task.kind, []) if url != action]

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Origin": "https://mooc1.chaoxing.com",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }

        for url in candidates:
            self._limit_rate()
            try:
                resp = session.post(url, data=payload, headers=headers)
            except Exception as exc:
                logger.warning(f"[自动答题] 提交请求异常({url[:60]}): {exc}")
                continue

            if resp.status_code != 200:
                logger.warning(f"[自动答题] 提交返回 {resp.status_code}, 尝试下一个接口")
                continue

            if self._is_success(resp):
                action_name = "提交" if submit else "保存"
                logger.info(f"[自动答题] {action_name}成功 -> {task.title}")
                return True

            logger.warning(f"[自动答题] 提交未被确认, 响应片段: {resp.text[:120]}")
            self._dump(resp.text, f"submit_{task.kind}_{task.title}")

        return False

    @staticmethod
    def _form_action(html: str, current_url: str) -> Optional[str]:
        soup = BeautifulSoup(html, "lxml")
        form = soup.find("form")
        if not form:
            return None
        action = (form.get("action") or "").strip()
        if not action or action.startswith("javascript"):
            return None
        return urljoin(current_url, action)

    @staticmethod
    def _is_success(resp) -> bool:
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            text = resp.text or ""
            return any(word in text for word in ("成功", "success", "已提交"))
        if isinstance(data, dict):
            if data.get("status") in (True, 1, "1", "true"):
                return True
            message = str(data.get("msg") or data.get("message") or "")
            return any(word in message for word in ("成功", "已提交", "已完成"))
        return False

    # ------------------------------------------------------------ 工具
    def _limit_rate(self):
        limiter = getattr(self.chaoxing, "rate_limiter", None)
        if limiter is None:
            time.sleep(0.5)
            return
        limiter.limit_rate(random_time=True, random_max=0.3)

    def _dump(self, html: str, name: str, when_no_task: bool = False):
        """把原始 HTML 存盘, 方便对着真实页面改解析

        正常情况下只在 debug=true 时存盘; 但如果列表页一条都没解析出来,
        说明页面结构变了, 这时候无条件存一份, 否则没法定位问题。
        同名文件只写一次, 避免多个候选地址反复覆盖同一个文件。
        """
        if not self.config.get("debug") and not when_no_task:
            return
        if name in self._dumped:
            return
        self._dumped.add(name)
        try:
            directory = self.config["debug_dir"]
            os.makedirs(directory, exist_ok=True)
            safe_name = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", name)[:60]
            path = os.path.join(directory, f"{safe_name}.html")
            with open(path, "w", encoding="utf8") as fp:
                fp.write(html)
            logger.info(f"[自动答题] 原始页面已保存: {path}")
        except Exception as exc:
            logger.debug(f"[自动答题] 保存调试页面失败: {exc}")
