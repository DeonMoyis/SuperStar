# -*- coding: utf-8 -*-
"""播放与作答节奏控制

两个约束都是为了对齐超星服务端的时长/频率校验:

1. 倍速固定为 1.0
   上报给服务端的进度是按「视频内时间」算的, 而校验看的是「真实时间跨度」。
   2 倍速下真实 30 分钟走完 60 分钟的视频, 服务端很容易判定为异常播放,
   表现为任务点不计入、重复回滚、甚至整门课的成绩不出。

2. 同一时刻只允许一个视频推进进度 (PlaybackGate)
   原实现里章节并发(jobs)和章节内任务点并发(ThreadPoolExecutor)会让
   N 个视频同时"播放", 真实耗时只有视频总时长的 1/N。
   这不叫倍速, 但效果和倍速一样, 而且更隐蔽 —— 用户会看到
   「明明开的是 1 倍速, 运行时长却远小于课程时长」。

   注意: 这不是限速, 而是把并发播放改成排队播放。文档/阅读这类
   不占时长的任务点仍然可以并发, 只有视频需要排队。

3. 同一时刻只允许一个测验在作答/提交 (AnswerGate)
   章节内的任务点由 5 个线程并发处理, 一个章节若有 3 个测验,
   就会同时抓题、同时发 AI 请求、同时 POST 提交, 容易触发风控。
   这里和视频一样排队, 代价是几十秒的顺序延迟。
"""
import threading
import time
from contextlib import contextmanager

from api.logger import logger

# 唯一允许的播放倍速。所有入口的倍速配置都会被压到这个值。
FORCED_SPEED = 1.0


def normalize_speed(value, logger_prefix: str = "播放倍速") -> float:
    """把任意倍速配置压到 1.0, 并在被下调时给出提示

    Args:
        value: 来自配置文件或命令行的原始值, 可能为 str/float/None
        logger_prefix: 日志前缀, 便于定位是哪个配置项触发的

    Returns:
        永远返回 FORCED_SPEED
    """
    try:
        requested = float(value)
    except (TypeError, ValueError):
        return FORCED_SPEED

    if abs(requested - FORCED_SPEED) > 1e-6:
        logger.warning(
            f"{logger_prefix}配置为 {requested:g}x, 已强制改为 {FORCED_SPEED:g}x "
            f"(超星对时长校验严格, 倍速有漏任务点的风险)"
        )
    return FORCED_SPEED


class SerialGate:
    """通用串行闸门: 同一时刻只允许一个持有者

    视频播放和章节测验都用它, 区别只在日志措辞:
    - 视频: 并发播放会把真实耗时压缩成 1/N, 等于变相倍速
    - 测验: 并发提交容易触发风控, 也可能同时打多路 AI 请求

    用法:
        with playback_gate.hold("第一章 视频"):
            ...推进播放进度...
    """

    def __init__(
        self,
        resource_name: str = "播放位",
        unit_name: str = "视频",
        busy_text: str = "正在播放",
        stat_label: str = "播放",
    ):
        self._sem = threading.Semaphore(1)
        self._stat_lock = threading.Lock()
        self._holder: str | None = None
        self.resource_name = resource_name
        self.unit_name = unit_name
        self.busy_text = busy_text
        self.stat_label = stat_label
        # 累计真实占用耗时(秒), 用于结束时核对是否真的按 1 倍速走完
        self.played_seconds = 0.0
        # 因排队而等待的累计时长(秒)
        self.waited_seconds = 0.0

    @property
    def holder(self) -> str | None:
        """当前占用闸门的任务名, 空闲时为 None"""
        return self._holder

    @contextmanager
    def hold(self, name: str = "", should_stop=None):
        """占用闸门

        Args:
            name: 任务名, 仅用于日志
            should_stop: 可选回调, 返回 True 表示不必再等闸门了(例如到达运行时长上限)。
                排队中被拒绝时 yield False, 调用方据此跳过该任务;
                已经拿到闸门的任务不受影响, 会正常做完。

        Yields:
            bool: 是否成功拿到闸门
        """
        waited = 0.0
        announced = False
        # 每 1 秒探一次: 既能及时拿到闸门, 也能在收到停止信号后 1 秒内退出等待
        while not self._sem.acquire(timeout=1):
            waited += 1
            if should_stop is not None and should_stop():
                logger.info(f"不再等待{self.resource_name}, 跳过该{self.unit_name}: {name}")
                yield False
                return
            if not announced:
                logger.info(f"当前有{self.unit_name}{self.busy_text}, 排队等待: {name}")
                announced = True
            elif int(waited) % 60 < 2:
                logger.info(f"仍在排队等待{self.resource_name}(已等待 {waited:.0f}s): {name}")

        self._holder = name
        started = time.time()
        try:
            yield True
        finally:
            elapsed = time.time() - started
            with self._stat_lock:
                self.played_seconds += elapsed
                self.waited_seconds += waited
            self._holder = None
            self._sem.release()

    def reset_stats(self):
        with self._stat_lock:
            self.played_seconds = 0.0
            self.waited_seconds = 0.0

    def stats(self) -> str:
        with self._stat_lock:
            return (
                f"累计{self.stat_label} {self.played_seconds / 60:.1f} 分钟, "
                f"排队等待 {self.waited_seconds / 60:.1f} 分钟"
            )


class PlaybackGate(SerialGate):
    """视频播放闸门: 同一时刻只允许一个视频在推进进度"""

    def __init__(self):
        super().__init__(
            resource_name="播放位", unit_name="视频", busy_text="正在播放", stat_label="播放"
        )


class AnswerGate(SerialGate):
    """作答闸门: 同一时刻只允许一个测验/考试在作答和提交

    章节内的任务点原本是 5 个线程并发的, 一个章节有 3 个测验就会同时
    抓题、同时发 AI 请求、同时 POST 提交。超星对提交频率敏感,
    所以这里和视频一样排队。
    """

    def __init__(self):
        super().__init__(
            resource_name="作答位", unit_name="测验", busy_text="正在作答", stat_label="作答"
        )


# 全局单例: 所有线程共用同一个播放位
playback_gate = PlaybackGate()

# 全局单例: 所有线程共用同一个作答位
answer_gate = AnswerGate()


def format_duration(seconds: float) -> str:
    """秒数 -> h:mm:ss / m:ss, 仅用于日志, 不参与进度计算"""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"
