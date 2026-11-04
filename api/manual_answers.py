# -*- coding: utf-8 -*-
"""人工校订答案（答案本）

为什么需要它
------------
大模型答错是常态，尤其是判断题和「以下哪项**不属于**」这类反向题。答错之后，
项目里现有的两套缓存都救不回来：

| 缓存 | 问题 |
| --- | --- |
| `ai_cache.json` | 被 `.gitignore` 忽略，Actions 里又由 `actions/cache` 恢复，**你提交上去的版本会被缓存覆盖**；key 里还带 `CACHE_VERSION` + 模型名，改提示词就整批失效 |
| `cache.json`（题库） | 只在配了 `[tiku] provider` 时才启用；key 是原始题干，多一个空格就 miss |

所以这里放一份**人为钉死的答案**，跟着仓库走：

* 优先级最高 —— 人工 > 题库 > 大模型 > 随机；
* 匹配按「归一化题干」，题号、题型标记、空格、中英文标点、分数后缀都不影响；
* 不受 `CACHE_VERSION` / 模型 / Actions 缓存影响，改完下一次运行立刻生效；
* 提交进仓库后，别人 clone 下来跑也是这份答案。

文件格式（`manual_answers.json`）
--------------------------------
```json
{
  "version": 1,
  "entries": [
    {
      "title": "经济实力影响和制约着国家的国防、科技、教育等实力，所以说，经济安全事关国家的国际地位。",
      "type": "judgement",
      "answer": "错误",
      "course": "形势与政策",
      "note": "AI 答成正确了"
    }
  ]
}
```

字段说明：

* `title` —— 题干。写完整最稳，也可以只写开头一段（≥ 8 个字，按包含匹配）；
* `answer` —— 必填，人类写法即可：
  * 判断题：`正确` / `错误` / `对` / `错` / `√` / `×` / `true` / `false`
  * 选择题：`A` / `ABD`，或者直接抄选项原文（会自动对到字母）
  * 填空题：多个空用 `|` 或换行分隔
* `type` —— 可选，帮助解析答案；不填就按题目本身的题型解析；
* `course` / `note` —— 可选，只给人看。

想批量生成骨架，用 `python tools/extract_answers.py`（从运行日志里把做过的题抽出来）。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from difflib import SequenceMatcher
from typing import Any, Optional

from loguru import logger

__all__ = [
    "DEFAULT_ANSWERS_FILE",
    "ManualAnswerBook",
    "configure_default_answer_book",
    "get_default_answer_book",
    "lookup_manual_answer",
    "normalize_title",
]

DEFAULT_ANSWERS_FILE = "manual_answers.json"
BOOK_VERSION = 1

# 题干里跟"哪道题"无关的东西，比较前统一抹掉
_IMG_TAG = re.compile(r"<img[^>]*>", re.IGNORECASE)
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\ufeff]")
# 题号: 只有「数字 + 分隔符」或「数字紧跟【/（」才当题号，避免吃掉 "1949年..."
_LEADING_NO = re.compile(r"^\s*\d{1,3}\s*(?:[、.．,，)）:：]\s*|[【\[（(]|$)")
# 题型标记: 【判断题】【单选题】之类
_LEADING_TYPE = re.compile(
    r"^\s*[【\[（(]?\s*(?:判断题|单选题|多选题|不定项选择题|填空题|简答题|论述题|名词解释|判断|单选|多选)"
    r"\s*[】\]）)]?\s*"
)
# 末尾的「（1.0分）」
_SCORE_SUFFIX = re.compile(r"[（(]\s*\d+(?:\.\d+)?\s*分\s*[)）]\s*$")
# 全部标点、空白（中英文都要，题干比对时它们没有任何意义）
_DROP_CHARS = "，。、；：？！“”‘’（）〔〕【】《》〈〉[]{}<>·…—～~－_.,;:?!\"'`|/\\*#＆$%^&+=@\u3000 \t\r\n"


def normalize_title(text: Any) -> str:
    """把题干归一化成「只保留文字」的形式，用来比对

    题号、题型标记、分数后缀、大小写、全部标点空白都会被抹掉，这样
    「1【判断题】经济安全很重要。（）  （1.0分）」和「经济安全很重要」
    会被认成同一道题。
    """
    raw = str(text or "")
    if not raw:
        return ""
    # 图片题: 图片标签不参与比对(URL 里每次都可能带不同签名)
    raw = _IMG_TAG.sub("", raw)
    raw = _ZERO_WIDTH.sub("", raw)

    # 前缀可能叠加: "1【判断题】…" 要先剥两遍
    for _ in range(3):
        before = raw
        raw = _LEADING_NO.sub("", raw, count=1)
        raw = _LEADING_TYPE.sub("", raw, count=1)
        raw = _SCORE_SUFFIX.sub("", raw, count=1)
        if raw == before:
            break

    raw = raw.translate(str.maketrans("", "", _DROP_CHARS))
    return raw.strip().lower()


def _answer_text(entry: dict) -> str:
    value = entry.get("answer")
    if isinstance(value, (list, tuple)):
        return "\n".join(str(item) for item in value)
    return "" if value is None else str(value)


class ManualAnswerBook:
    """人工校订答案本（线程安全，文件改动后自动重载）"""

    #: 包含匹配的最短长度，太短的题干片段容易误伤别的题
    MIN_CONTAINS_LEN = 8
    #: 相似度兜底的阈值（题干有错别字/改版时用）
    MIN_RATIO = 0.95

    def __init__(self, path: str = DEFAULT_ANSWERS_FILE):
        self.path = path
        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self._index: dict[str, dict] = {}
        self._mtime: float = 0.0
        self._checked_at: float = 0.0
        self.loaded = False
        self.warnings: list[str] = []
        self._load()

    # ------------------------------------------------------------ 读写
    def _load(self) -> None:
        if not self.path or not os.path.isfile(self.path):
            # 没有这个文件是正常情况(不配置就不用), 不报错
            self.loaded = False
            return
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = 0.0
        try:
            with open(self.path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, ValueError) as exc:
            logger.warning("[人工答案] 读取 {} 失败, 本次不使用人工答案: {}", self.path, exc)
            self.loaded = False
            return

        entries_raw = data.get("entries") if isinstance(data, dict) else data
        if not isinstance(entries_raw, list):
            logger.warning("[人工答案] {} 格式不对(需要 entries 数组), 本次忽略", self.path)
            self.loaded = False
            return

        entries: list[dict] = []
        index: dict[str, dict] = {}
        warnings: list[str] = []
        for line_no, item in enumerate(entries_raw, start=1):
            if not isinstance(item, dict):
                warnings.append(f"第 {line_no} 条不是对象, 已跳过")
                continue
            key = normalize_title(item.get("title"))
            if not key:
                warnings.append(f"第 {line_no} 条缺少 title, 已跳过")
                continue
            if not _answer_text(item).strip():
                warnings.append(f"第 {line_no} 条({item.get('title')[:20]}) 没有 answer, 已跳过")
                continue
            if item.get("disabled"):
                # 模板里的示例条目: 留着给人看, 但不参与匹配
                continue
            entry = dict(item)
            entry["_key"] = key
            # 同一道题写了多次: 后面的覆盖前面的
            if key in index:
                old = index[key]
                entries[entries.index(old)] = entry
            else:
                entries.append(entry)
            index[key] = entry

        with self._lock:
            self._entries = entries
            self._index = index
            self._mtime = mtime
            self._checked_at = time.time()
            self.loaded = True
            self.warnings = warnings

        if warnings:
            for msg in warnings[:5]:
                logger.warning("[人工答案] {}: {}", os.path.basename(self.path), msg)
        logger.info(
            "[人工答案] 已加载 {}: {} 条人工校订答案(优先级最高, 压过题库与大模型)",
            self.path, len(entries),
        )

    def reload_if_changed(self) -> None:
        """文件被改过就重新加载（本地运行时可边跑边改）"""
        if not self.path:
            return
        now = time.time()
        if now - self._checked_at < 2:
            return
        with self._lock:
            self._checked_at = now
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            return
        if abs(mtime - self._mtime) > 1e-6:
            logger.info("[人工答案] 检测到 {} 有改动, 重新加载", self.path)
            self._load()

    # ------------------------------------------------------------ 查询
    def lookup(self, question: dict) -> Optional[dict]:
        """给一道题找人工答案，找不到返回 None"""
        if not isinstance(question, dict):
            return None
        self.reload_if_changed()

        key = normalize_title(question.get("title"))
        if not key:
            return None

        with self._lock:
            index = self._index
            entries = list(self._entries)

        # 1) 完全一致(归一化后)
        hit = index.get(key)
        if hit:
            return hit

        # 2) 人工答案写的是题干的一部分(至少 MIN_CONTAINS_LEN 个字), 取最长的那条
        best: Optional[dict] = None
        for entry in entries:
            entry_key = entry.get("_key") or ""
            if len(entry_key) < self.MIN_CONTAINS_LEN:
                continue
            if entry_key in key:
                if best is None or len(entry_key) > len(best.get("_key") or ""):
                    best = entry
        if best:
            return best

        # 3) 相似度兜底: 题干改了个标点/错别字/版本微调
        best_ratio = 0.0
        for entry in entries:
            entry_key = entry.get("_key") or ""
            if len(entry_key) < self.MIN_CONTAINS_LEN:
                continue
            # 长度差太大直接跳过, 省点计算
            if abs(len(entry_key) - len(key)) > max(6, int(len(key) * 0.2)):
                continue
            ratio = SequenceMatcher(None, entry_key, key).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best = entry
        if best is not None and best_ratio >= self.MIN_RATIO:
            logger.info("[人工答案] 按相似度 {:.2f} 命中: {}", best_ratio, question.get("title", "")[:40])
            return best
        return None

    def stats(self) -> dict:
        with self._lock:
            return {"entries": len(self._entries), "loaded": self.loaded, "path": self.path}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


# ---------------------------------------------------------------- 全局单例
_default_book: Optional[ManualAnswerBook] = None
_book_lock = threading.Lock()


def configure_default_answer_book(path: Optional[str] = None,
                                  enabled: bool = True) -> Optional[ManualAnswerBook]:
    """配置全局答案本; enabled=False 表示本次不用人工答案"""
    global _default_book
    with _book_lock:
        if not enabled:
            _default_book = None
        else:
            _default_book = ManualAnswerBook(path or DEFAULT_ANSWERS_FILE)
    return _default_book


def get_default_answer_book() -> Optional[ManualAnswerBook]:
    with _book_lock:
        return _default_book


def lookup_manual_answer(question: dict) -> Optional[dict]:
    """没人配置答案本时返回 None（同一道题安静地走题库/大模型）"""
    book = get_default_answer_book()
    if book is None:
        return None
    return book.lookup(question)


if __name__ == "__main__":  # 手动看一眼答案本里有什么
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ANSWERS_FILE
    answer_book = ManualAnswerBook(target)
    print(f"{target}: {answer_book.stats()}")
    for item in answer_book._entries:
        print(f" - [{item.get('type') or '?'}] {_answer_text(item)} <- {str(item.get('title'))[:40]}")
