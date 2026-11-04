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
_TITLE_KEYS = ("title", "question", "content", "stem", "qtitle", "name", "description")
# 选项字段候选
_OPTION_KEYS = ("options", "option", "choices", "optionList")
# 题型字段候选
_TYPE_KEYS = ("type", "questionType", "qtype", "question_type")

# 拉取「本视频有哪些弹窗题」的接口, 新老两版都试一次
QUIZ_LIST_URLS = (
    "https://mooc1.chaoxing.com/mooc-ans/richvideo/initdatawithviewerV2",
    "https://mooc1.chaoxing.com/mooc-ans/richvideo/initdatawithviewer",
)
# 提交答案时的域名(validationUrl 是相对路径, 由服务端下发, 不写死具体路径)
QUIZ_HOST = "https://mooc1.chaoxing.com"

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
    raw_options = None
    for k in _OPTION_KEYS:
        v = node.get(k)
        if isinstance(v, str) and v.strip():
            raw_options = v.strip()
            break
        if isinstance(v, list) and v:
            raw_options = v
            break

    options = ""
    option_list: list[dict] = []
    if isinstance(raw_options, str):
        options = raw_options
        option_list = [
            {"name": line.strip()[:1], "description": line}
            for line in raw_options.split("\n") if line.strip()
        ]
    elif isinstance(raw_options, list):
        parts = []
        for idx, item in enumerate(raw_options):
            if isinstance(item, dict):
                name = str(item.get("name") or chr(65 + idx))
                desc = str(item.get("description") or item.get("content") or "")
                option_list.append({"name": name, "description": desc})
                parts.append(f"{name}.{desc}" if desc else name)
            else:
                name = chr(65 + idx)
                option_list.append({"name": name, "description": str(item)})
                parts.append(f"{name}.{item}")
        options = "\n".join(parts)

    qtype = _normalize_type(_first_str(node, _TYPE_KEYS))
    if not qtype:
        qtype = "single" if options else "judgement"

    pop_time = node.get("startTime")
    if pop_time is None:
        pop_time = node.get("time") or node.get("popTime")
    try:
        pop_time = float(pop_time) if pop_time is not None else None
    except (TypeError, ValueError):
        pop_time = None

    return {
        "id": str(node.get("id") or node.get("questionId") or node.get("resourceId") or ""),
        "title": _first_str(node, _TITLE_KEYS),
        "options": options,
        "type": qtype,
        "time": pop_time,
        # 提交答案要用的字段, 由服务端下发, 不同课程版本路径不一样,
        # 所以这里原样保留而不是拼一个固定 URL
        "validationUrl": str(node.get("validationUrl") or node.get("validateUrl") or ""),
        "resourceId": str(node.get("resourceId") or node.get("resourceid") or ""),
        "optionList": option_list,
        # 与章节测验保持同一套结构, 好直接丢给 AnswerEngine
        "answerField": {},
    }


def option_name_for_letter(quiz: dict, letter: str) -> str:
    """把 A/B/C 换成服务端认的选项 name

    弹窗题的选项提交的是 `name` 字段(不是字母), 大多数情况下它也正好是 "A"/"B",
    但有的课程下发的是选项 id, 所以按顺序取, 别直接拿字母去提交。
    """
    letter = (letter or "").strip().upper()[:1]
    options = quiz.get("optionList") or []
    if letter:
        for item in options:
            if str(item.get("name", "")).strip().upper() == letter:
                return str(item["name"]).strip()
    if len(letter) == 1:
        idx = ord(letter) - 65
        if 0 <= idx < len(options):
            name = str(options[idx].get("name") or "").strip()
            if name:
                return name
    return letter


def parse_quiz_list(payload: Any) -> list[dict]:
    """解析 initdatawithviewer(V2) 的响应, 拿出弹窗题列表

    响应形如 `[{"datas": [ {startTime, validationUrl, resourceId, options: [...]}, ... ]}]`,
    外层偶尔是字典而不是列表, 两种都兼容。按弹题时间升序返回。
    """
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        return []

    quizzes: list[dict] = []
    seen: set[str] = set()
    for group in payload:
        if not isinstance(group, dict):
            continue
        datas = group.get("datas")
        if not isinstance(datas, list):
            continue
        for raw in datas:
            if not isinstance(raw, dict):
                continue
            quiz = _normalize_question(raw)
            if not quiz["title"] and not quiz["options"]:
                continue
            if not quiz["validationUrl"]:
                # 没有提交地址的条目(有时是讲义/资料), 做了也提交不了, 直接跳过
                continue
            key = quiz["id"] or quiz["title"][:80]
            if key in seen:
                continue
            seen.add(key)
            quizzes.append(quiz)

    quizzes.sort(key=lambda item: item.get("time") or 0.0)
    return quizzes


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
