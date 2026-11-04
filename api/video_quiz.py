# -*- coding: utf-8 -*-
"""视频内嵌题(弹窗题)的识别与诊断

超星(学习通)的部分视频会在播放到某个时间点时弹出小题, 不答就暂停;
服务端也会因此判定任务点未完成 —— 表现为「进度已经到片尾, isPassed 仍然是 false」。

这类题目跟着章节卡片一起从 /mooc-ans/knowledge/cards 的 mArg 里下发,
但不同课程/版本的字段名不一致(question / questions / property.question ...),
所以这里做**结构化扫描**而不是写死路径: 只要某个字典同时具备
「题干」和「选项或题型」, 就认为它是题目。

同时提供 summarize_video_card(), 用来回答一个关键问题:
这个视频为什么刷不过去 —— 是有弹窗题, 还是开了人脸采集, 还是别的。
"""
from __future__ import annotations

import json
from typing import Any

# 题干字段候选(按优先级)
_TITLE_KEYS = ("title", "question", "content", "stem", "qtitle", "name")
# 选项字段候选
_OPTION_KEYS = ("options", "option", "choices", "optionList")
# 题型字段候选
_TYPE_KEYS = ("type", "questionType", "qtype", "question_type")

# 已知题型 -> 归一化
_TYPE_ALIASES = {
    "single": "single",
    "singlechoice": "single",
    "单选题": "single",
    "multiple": "multiple",
    "multichoice": "multiple",
    "多选题": "multiple",
    "judgement": "judgement",
    "judge": "judgement",
    "truefalse": "judgement",
    "判断题": "judgement",
    "completion": "completion",
    "fill": "completion",
    "填空题": "completion",
}

# 卡片里可能承载「视频内嵌题」的字段名
_QUESTION_CONTAINER_KEYS = (
    "question", "questions", "questionList", "quiz", "quizs",
    "videoQuestion", "videoQuestions", "exam", "examList", "inlineQuestion",
)


def _first_str(node: dict, keys) -> str:
    for k in keys:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def _normalize_type(raw: str) -> str:
    key = str(raw or "").strip().lower().replace(" ", "").replace("-", "")
    return _TYPE_ALIASES.get(key, "")


def _looks_like_question(node: Any) -> bool:
    """同时具备「题干」和「选项/题型」才认为是题目

    只认题干会把 name/title 之类的普通字段全误判成题目, 所以必须有第二重证据。
    """
    if not isinstance(node, dict):
        return False
    if not _first_str(node, _TITLE_KEYS):
        return False
    has_options = any(node.get(k) for k in _OPTION_KEYS)
    qtype = _normalize_type(_first_str(node, _TYPE_KEYS))
    if has_options and (qtype or len(str(node.get("options") or "")) > 3):
        return True
    # 判断题经常没有 options
    return qtype == "judgement"


def _normalize_question(node: dict) -> dict:
    options = ""
    for k in _OPTION_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            options = v.strip()
            break
        if isinstance(v, list) and v:
            options = "\n".join(str(x) for x in v)
            break

    qtype = _normalize_type(_first_str(node, _TYPE_KEYS))
    if not qtype:
        qtype = "single" if options else "judgement"

    pop_time = node.get("time") or node.get("startTime") or node.get("popTime")
    try:
        pop_time = float(pop_time) if pop_time is not None else None
    except (TypeError, ValueError):
        pop_time = None

    return {
        "id": str(node.get("id") or node.get("questionId") or ""),
        "title": _first_str(node, _TITLE_KEYS),
        "options": options,
        "type": qtype,
        "time": pop_time,
        # 与章节测验保持同一套结构, 好直接丢给 AnswerEngine
        "answerField": {},
    }


def find_inline_questions(raw_card: Any, max_depth: int = 6) -> list[dict]:
    """从视频任务卡片里递归找出所有疑似弹窗题

    Args:
        raw_card: /knowledge/cards 里该视频任务点的原始 JSON
        max_depth: 递归深度上限, 防止异常结构把调用栈打穿

    Returns:
        归一化后的题目列表, 没有则返回 []。同一道题(按 id 或题干)只保留一份。
    """
    if not isinstance(raw_card, (dict, list)):
        return []

    found: list[dict] = []
    seen_nodes: set[int] = set()
    seen_keys: set[str] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        if id(node) in seen_nodes:
            return
        seen_nodes.add(id(node))

        if _looks_like_question(node):
            q = _normalize_question(node)
            key = q["id"] or q["title"][:80]
            if key and key not in seen_keys:
                seen_keys.add(key)
                found.append(q)

        # 优先只钻「可能装题目」的字段, 减少把讲义正文误判成题目
        for k, v in node.items():
            if isinstance(v, (dict, list)):
                if k in _QUESTION_CONTAINER_KEYS:
                    walk(v, depth + 1)
                elif depth == 0:
                    # 顶层容器(property/attachment 等)继续往下找一层
                    walk(v, depth + 1)

    walk(raw_card, 0)
    return found


def summarize_video_card(raw_card: Any) -> dict:
    """汇总一个视频卡片的诊断信息

    用来回答「这个视频为什么刷不过去」: 有没有弹窗题、有没有开人脸采集。
    """
    if not isinstance(raw_card, dict):
        return {"ok": False, "reason": "卡片数据缺失"}

    questions = find_inline_questions(raw_card)
    property_data = raw_card.get("property") or {}
    face_enc = raw_card.get("videoFaceCaptureEnc") or ""

    summary = {
        "ok": True,
        "top_keys": sorted(raw_card.keys())[:24],
        "property_keys": sorted(property_data.keys())[:24] if isinstance(property_data, dict) else [],
        "has_face_capture": bool(face_enc),
        "question_count": len(questions),
        "questions": questions,
    }
    if questions:
        summary["reason"] = "卡片里带弹窗题"
    elif face_enc:
        summary["reason"] = "该视频开启人脸采集, 纯 HTTP 无法完成"
    else:
        summary["reason"] = "卡片里没发现弹窗题, 也不像人脸采集(可能是服务端状态或风控)"
    return summary


def dump_json(data: Any) -> str:
    """稳定的 JSON 序列化, 用于落盘诊断"""
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)
