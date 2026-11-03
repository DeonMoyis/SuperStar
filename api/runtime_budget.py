# -*- coding: utf-8 -*-
"""单次运行时长预算（时间盒）

有些学校明确要求「不能一次性把整门课刷完」，需要分多次、隔一段时间完成。
这个模块给程序加一个时间盒：

- 到达上限后**不再开始**新的章节 / 作业 / 考试；
- 已经开始的任务（正在播放的视频、正在作答的作业）**照常跑完**，不打断；
- 全部收尾后正常退出，进程返回码为 0。

配合 GitHub Actions 的 schedule(cron) 就是一套完整的「分段刷课」：
每次跑 N 分钟 → 退出 → 过几小时再跑一次，和真人学习的节奏基本一致。

注意: 上限是「软上限」。一个 90 分钟的视频从第 80 分钟开始播, 仍会播完,
所以实际结束时间可能是上限 + 单个任务的时长。
"""
import threading
import time

from api.logger import logger


class RuntimeBudget:
    """一次运行的时间预算

    limit_minutes 为 None / 0 / 负数时表示不限制，行为与之前完全一致。
    """

    def __init__(self, limit_minutes=None):
        self._lock = threading.Lock()
        self._started_at = time.time()
        self._limit = None
        self._notified = False
        self.configure(limit_minutes)

    # ------------------------------------------------------------------ 配置
    def configure(self, limit_minutes=None) -> "RuntimeBudget":
        """重新设置上限并重置计时起点

        Args:
            limit_minutes: 上限分钟数, 空值/0/非法值表示不限制
        """
        limit_seconds = None
        if limit_minutes not in (None, "", 0, "0", "0.0"):
            try:
                value = float(limit_minutes)
                if value > 0:
                    limit_seconds = value * 60
                elif value < 0:
                    logger.warning(f"运行时长上限为负数({value:g} 分钟), 已忽略, 本次不限制")
            except (TypeError, ValueError):
                logger.warning(f"运行时长上限配置无效({limit_minutes!r}), 已忽略, 本次不限制")

        with self._lock:
            self._started_at = time.time()
            self._limit = limit_seconds
            self._notified = False

        if limit_seconds is not None:
            logger.info(f"单次运行时长上限: {limit_seconds / 60:g} 分钟, 到点后正在执行的任务会跑完再退出")
        return self

    # ------------------------------------------------------------------ 状态
    @property
    def enabled(self) -> bool:
        return self._limit is not None

    @property
    def limit_seconds(self) -> float:
        return self._limit or 0.0

    @property
    def elapsed(self) -> float:
        return time.time() - self._started_at

    @property
    def remaining(self):
        """剩余秒数, 未设置上限时返回 None"""
        if self._limit is None:
            return None
        return max(0.0, self._limit - self.elapsed)

    @property
    def expired(self) -> bool:
        """是否已经到点（到点后只收尾, 不开新任务）"""
        if self._limit is None:
            return False
        return self.elapsed >= self._limit

    def should_stop(self) -> bool:
        """是否该停止领取新任务; 第一次命中时打一条提示, 避免刷屏"""
        if not self.expired:
            return False
        with self._lock:
            if not self._notified:
                self._notified = True
                logger.warning(
                    f"已达到单次运行时长上限 {self.limit_seconds / 60:g} 分钟 "
                    f"(实际已运行 {self.elapsed / 60:.1f} 分钟): "
                    f"不再开始新任务, 正在执行的任务跑完后退出"
                )
        return True

    # ------------------------------------------------------------------ 汇总
    def summary(self) -> str:
        minutes = self.elapsed / 60
        if not self.enabled:
            return f"本次运行 {minutes:.1f} 分钟 (未设置时长上限)"
        state = "已到上限" if self.expired else "未到上限"
        return (
            f"本次运行 {minutes:.1f} 分钟 / 上限 {self.limit_seconds / 60:g} 分钟 ({state})"
        )


# 全局单例: 各模块统一通过 runtime_budget 访问
runtime_budget = RuntimeBudget()
