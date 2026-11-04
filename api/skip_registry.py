# -*- coding: utf-8 -*-
"""任务点跳过记录簿

解决的问题
--------
有些任务点**靠程序永远做不完**, 最典型的是开了人脸采集的视频: 进度推到片尾,
服务端照样 `isPassed=false`, 甚至直接 403。更糟的是 Actions 会被中断后重开,
新进程对这件事一无所知, 于是又把同一个 15 分钟的视频从头播一遍 —— 白烧时长,
而这正是"看起来在跑、进度却不动"的来源。

这里把「哪个任务点、为什么过不去、能不能再试」落盘, 达到三个效果:

1. **程序记住** —— 下次运行读到 `permanent=True` 的条目直接跳过, 不再试;
2. **用户记住** —— 收尾汇总 / GitHub Step Summary 里逐条给出原因;
3. **只跳该跳的** —— `permanent=False`(403、风控、到点没做完)下次仍会重试,
   不会因为一次偶发失败就把任务点永久放弃。

原因码
------
`face_capture`     该视频开启人脸采集, 必须真人在网页端/App 过人脸  -> 永久
`forbidden`        上报进度被 403, 多为风控/会话失效                  -> 可重试
`server_reject`    进度到片尾但服务端不认通过                          -> 可重试
`inline_quiz`      视频内弹窗题没答对                                  -> 可重试
`runtime_budget`   单次运行时长上限到了                                -> 可重试
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Optional

from loguru import logger

DEFAULT_REGISTRY_FILE = "skip_registry.json"
# v2: 人脸采集的判定口径收紧了(不再「字段非空即人脸」), 旧结论一律作废。
# 见 _LEGACY_DROP_REASONS。
REGISTRY_VERSION = 2

# 原因码
REASON_FACE_CAPTURE = "face_capture"
REASON_FORBIDDEN = "forbidden"
REASON_SERVER_REJECT = "server_reject"
REASON_INLINE_QUIZ = "inline_quiz"
REASON_RUNTIME_BUDGET = "runtime_budget"

# 重试多少次都不会变的原因: 只有这些才值得"下次直接跳过"
PERMANENT_REASONS = {REASON_FACE_CAPTURE}

# 记录簿格式升级时, 哪些旧结论不能再沿用。
# v1 的 face_capture 是「videoFaceCaptureEnc 非空」就下的结论, 实测误判了大量
# 只是「视频中间有弹窗题」的课程, 导致整节视频被播前跳过、任务点空着。
_LEGACY_DROP_REASONS = {REASON_FACE_CAPTURE}

# 给用户看的中文说明
REASON_TEXT = {
    REASON_FACE_CAPTURE: "该视频开启人脸采集, 必须本人在网页端/App 里过人脸, 程序无法完成",
    REASON_FORBIDDEN: "上报进度一直被 403(风控或会话失效), 下次运行会重试",
    REASON_SERVER_REJECT: "进度已到片尾但服务端不认通过, 下次运行会重试",
    REASON_INLINE_QUIZ: "视频内弹窗题没做对, 下次运行会重试",
    REASON_RUNTIME_BUDGET: "本次运行时长上限到了, 下次运行会接着做",
}


def is_permanent(reason_code: str) -> bool:
    """这个原因是不是重试也没用的那种"""
    return reason_code in PERMANENT_REASONS


def make_key(kind: str, job: dict) -> str:
    """任务点的稳定标识: 优先 jobid, 没有就退到 objectid

    用 jobid 而不是名字, 是因为同一门课里完全可能有两个同名视频。
    """
    ident = ""
    for field in ("jobid", "objectid", "id"):
        value = job.get(field)
        if value:
            ident = str(value)
            break
    if not ident:
        ident = str(job.get("name") or "unknown")
    return f"{kind}:{ident}"


class SkipRegistry:
    """跳过的任务点记录簿(线程安全, 落盘可复用)"""

    def __init__(self, path: str = DEFAULT_REGISTRY_FILE, auto_save: bool = True):
        self.path = path
        self.auto_save = auto_save
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._dirty = False
        self._load()

    # ------------------------------------------------------------ 读写
    def _load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as exc:
            logger.warning("[跳过记录] 读取 {} 失败, 按空记录处理: {}", self.path, exc)
            return

        entries = data.get("entries") if isinstance(data, dict) else None
        if not isinstance(entries, dict):
            return
        try:
            version = int(data.get("version") or 1)
        except (TypeError, ValueError):
            version = 1

        dropped = 0
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            # 旧版本里「因人脸采集而永久跳过」的结论不可信, 丢弃后重新判定
            if version < REGISTRY_VERSION and entry.get("reason_code") in _LEGACY_DROP_REASONS:
                dropped += 1
                continue
            self._entries[str(key)] = entry
        if dropped:
            logger.warning(
                "[跳过记录] 丢弃 {} 条旧版「人脸采集」记录(判定口径已修正, 本次重新检查)",
                dropped,
            )
            self._dirty = True
        if self._entries:
            permanent = sum(1 for e in self._entries.values() if e.get("permanent"))
            logger.info(
                "[跳过记录] 已加载 {}: 共 {} 条记录, 其中 {} 条不会再重试",
                self.path, len(self._entries), permanent,
            )

    def save(self, force: bool = False) -> None:
        if not self.path:
            return
        with self._lock:
            if not self._dirty and not force:
                return
            payload = {
                "version": REGISTRY_VERSION,
                "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "entries": self._entries,
            }
            tmp = f"{self.path}.tmp"
            try:
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self.path)
                self._dirty = False
            except OSError as exc:
                logger.warning("[跳过记录] 写入 {} 失败: {}", self.path, exc)

    # ------------------------------------------------------------ 记录
    def record(
        self,
        kind: str,
        job: dict,
        reason_code: str,
        reason: str = "",
        course: str = "",
        permanent: Optional[bool] = None,
    ) -> dict:
        """记录一个跳过的任务点(同一个任务点重复记录只更新次数和时间)"""
        key = make_key(kind, job)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        if permanent is None:
            permanent = is_permanent(reason_code)

        with self._lock:
            entry = self._entries.get(key) or {
                "key": key,
                "kind": kind,
                "name": job.get("name"),
                "course": course,
                "first_seen": now,
                "attempts": 0,
            }
            entry.update({
                "name": job.get("name") or entry.get("name"),
                "course": course or entry.get("course") or "",
                "reason_code": reason_code,
                "reason": reason or REASON_TEXT.get(reason_code, reason_code),
                "permanent": bool(permanent),
                "last_seen": now,
                "attempts": int(entry.get("attempts", 0)) + 1,
            })
            self._entries[key] = entry
            self._dirty = True
        self.save()
        return entry

    # ------------------------------------------------------------ 查询
    def get(self, kind: str, job: dict) -> Optional[dict]:
        with self._lock:
            return self._entries.get(make_key(kind, job))

    def forget(self, kind: str, job: dict) -> bool:
        """删掉某条记录(复核后确认上次的结论是误判时用)"""
        key = make_key(kind, job)
        with self._lock:
            if key not in self._entries:
                return False
            entry = self._entries.pop(key)
            self._dirty = True
            self.save()
        logger.info(
            "[跳过记录] 撤销一条记录(复核后不成立): {} | {}",
            entry.get("name") or key, entry.get("reason") or entry.get("reason_code"),
        )
        return True

    def should_skip(self, kind: str, job: dict) -> Optional[dict]:
        """上次已判定为「重试也没用」时才返回记录, 否则 None"""
        entry = self.get(kind, job)
        if entry and entry.get("permanent"):
            return entry
        return None

    def entries(self, permanent_only: bool = False) -> list[dict]:
        with self._lock:
            items = list(self._entries.values())
        if permanent_only:
            items = [e for e in items if e.get("permanent")]
        return sorted(items, key=lambda e: str(e.get("last_seen", "")), reverse=True)

    def summary_lines(self, permanent_only: bool = False) -> list[str]:
        """收尾汇总用的一行一条说明"""
        lines = []
        for entry in self.entries(permanent_only=permanent_only):
            tag = "不再重试" if entry.get("permanent") else "下次会重试"
            line = f"{entry.get('name')}: {entry.get('reason')} ({tag}"
            if entry.get("attempts", 1) > 1:
                line += f", 已遇到 {entry['attempts']} 次"
            line += ")"
            lines.append(line)
        return lines

    def stats(self) -> dict:
        with self._lock:
            total = len(self._entries)
            permanent = sum(1 for e in self._entries.values() if e.get("permanent"))
        return {"total": total, "permanent": permanent, "retryable": total - permanent}


# ---------------------------------------------------------------- 全局单例
_default_registry: Optional[SkipRegistry] = None
_registry_lock = threading.Lock()


def configure_default_registry(path: Optional[str] = None,
                               registry: Optional[SkipRegistry] = None,
                               enabled: bool = True) -> Optional[SkipRegistry]:
    """配置全局记录簿; enabled=False 表示本次不记录"""
    global _default_registry
    with _registry_lock:
        if not enabled:
            _default_registry = None
        elif registry is not None:
            _default_registry = registry
        else:
            _default_registry = SkipRegistry(path or DEFAULT_REGISTRY_FILE)
    return _default_registry


def get_default_registry() -> Optional[SkipRegistry]:
    with _registry_lock:
        return _default_registry


def record_skip(kind: str, job: dict, reason_code: str, reason: str = "",
                course: str = "") -> Optional[dict]:
    """给全局记录簿写一条, 没配置就什么都不做"""
    registry = get_default_registry()
    if registry is None:
        return None
    return registry.record(kind, job, reason_code, reason=reason, course=course)


def should_skip(kind: str, job: dict) -> Optional[dict]:
    """上次已判定为「重试也没用」时返回那条记录"""
    registry = get_default_registry()
    if registry is None:
        return None
    return registry.should_skip(kind, job)


def forget_skip(kind: str, job: dict) -> bool:
    """撤销一条记录, 没配置记录簿时返回 False"""
    registry = get_default_registry()
    if registry is None:
        return False
    return registry.forget(kind, job)


def release_default_registry(save: bool = True) -> None:
    """收尾: 把记录簿落盘"""
    global _default_registry
    with _registry_lock:
        registry = _default_registry
        _default_registry = None
    if registry is not None and save:
        registry.save(force=True)


if __name__ == "__main__":  # 手动看一眼记录簿里有什么
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_REGISTRY_FILE
    reg = SkipRegistry(path)
    print(f"{path}: {reg.stats()}")
    for line in reg.summary_lines():
        print(" -", line)
