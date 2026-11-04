# -*- coding: utf-8 -*-
"""视频内嵌题(弹窗题)识别测试

背景: 每小节视频播放中途可能弹出小题, 不答视频就停住, 服务端也不会把
任务点判为通过 —— 表现是「进度到片尾了, isPassed 依然是 false」。
题目数据跟着章节卡片一起下发, 但字段名不稳定, 所以用结构化扫描。
这里把「认得出来」和「不乱认」两件事都钉住。
"""
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.base as base  # noqa: E402
from api.video_quiz import (  # noqa: E402
    find_inline_questions,
    option_name_for_letter,
    parse_quiz_list,
    summarize_video_card,
)


def test_find_questions_in_card():
    """典型的卡片结构: 题目挂在 property 下面"""
    card = {
        "jobid": "1664437997416549",
        "objectId": "2670a79b",
        "type": "video",
        "property": {
            "name": "2.3 政治安全面临的威胁与挑战.mp4",
            "duration": 534,
            "questions": [
                {
                    "id": "q1",
                    "title": "政治安全面临的威胁主要表现在哪些方面?",
                    "options": "A.颜色革命\nB.意识形态渗透\nC.领土争端",
                    "type": "multiple",
                    "time": 120,
                }
            ],
        },
    }
    questions = find_inline_questions(card)
    assert len(questions) == 1, questions
    q = questions[0]
    assert q["id"] == "q1"
    assert q["type"] == "multiple"
    assert q["time"] == 120.0
    assert "A.颜色革命" in q["options"]
    assert q["answerField"] == {}, "要留出 answerField 给答题引擎回填"
    print("[ok] 能从卡片里认出一节弹窗题")


def test_no_false_positive_on_plain_card():
    """普通视频卡片不能被误判成「有弹窗题」"""
    card = {
        "jobid": "1",
        "objectId": "abc",
        "type": "video",
        "attDuration": "900",
        "videoFaceCaptureEnc": "",
        "property": {
            "name": "1.2 总体国家安全观内涵.mp4",
            "duration": "457",
            "rt": "0.9",
            "objectid": "abc",
        },
    }
    assert find_inline_questions(card) == [], "普通卡片不该识别出题目"

    summary = summarize_video_card(card)
    assert summary["question_count"] == 0
    assert summary["has_face_capture"] is False
    assert "没发现弹窗题" in summary["reason"]
    print("[ok] 普通视频卡片不会误判")


def test_detect_face_capture():
    """开了人脸采集的视频要能单独标出来 —— 这种纯 HTTP 做不到"""
    card = {
        "jobid": "2",
        "objectId": "def",
        "type": "video",
        "videoFaceCaptureEnc": "701f717922e30129e914bcec53d18d3f",
        "property": {"name": "2.2 政治安全的主要内容.mp4", "duration": "900"},
    }
    summary = summarize_video_card(card)
    assert summary["has_face_capture"] is True
    assert "人脸采集" in summary["reason"], summary["reason"]
    print("[ok] 能识别出人脸采集")


def test_face_capture_needs_real_evidence():
    """只有「字段非空」不算人脸采集 —— 这是曾经的误判源头

    旧实现: `videoFaceCaptureEnc` 非空就判人脸, 结果把一批「视频中间插了一道题」
    的课程整节跳过。真实卡片里这个字段经常带着 "0" 之类的占位值。
    """
    cases = [
        ({"videoFaceCaptureEnc": ""}, False, "空串"),
        ({"videoFaceCaptureEnc": "0"}, False, "字符串 0"),
        ({"videoFaceCaptureEnc": "abc"}, False, "短占位串"),
        ({"videoFaceCaptureEnc": "1234567890"}, False, "纯数字短串"),
        ({"videoFaceCapture": 0}, False, "开关为 0"),
        ({"videoFaceCapture": "0"}, False, "开关为字符串 0"),
        ({"videoFaceCapture": 1}, True, "开关为 1"),
        ({"videoFaceCapture": "1"}, True, "开关为字符串 1"),
        ({"videoFaceCaptureEnc": "701f717922e30129e914bcec53d18d3f"}, True, "真密文"),
    ]
    for fields, expected, note in cases:
        job = {"jobid": "x", "name": "视频.mp4"}
        job.update(fields)
        got = base.has_face_capture(job)
        assert got is expected, f"{note}: 期望 {expected}, 实际 {got} ({fields})"
    print("[ok] 人脸判定: 只认明确开关和真密文, 占位值不再误判")


def test_face_capture_with_nested_property():
    """开关也可能放在 property 里"""
    card = {
        "jobid": "3",
        "type": "video",
        "property": {"name": "v.mp4", "videoFaceCapture": 1},
    }
    assert base.has_face_capture({"jobid": "3", "raw_card": card}) is True

    plain = {"jobid": "4", "type": "video", "property": {"name": "v.mp4", "videoFaceCapture": 0}}
    assert base.has_face_capture({"jobid": "4", "raw_card": plain}) is False
    print("[ok] 人脸判定: 能读 property 里的开关, 关闭时不误判")


def test_question_dedup():
    """同一道题在多个位置出现(容器+具体节点)只保留一份"""
    question = {
        "id": "q9",
        "title": "党的十八届三中全会决定成立什么?",
        "options": "A.中央国家安全委员会\nB.中央军委\nC.国务院",
        "type": "single",
    }
    card = {
        "jobid": "3",
        "type": "video",
        "property": {"name": "v.mp4", "question": question},
        "questions": [question],
    }
    questions = find_inline_questions(card)
    assert len(questions) == 1, f"应该去重成 1 道, 实际 {len(questions)}"
    print("[ok] 重复出现的同一道题只算一次")


def test_judgement_without_options():
    """判断题经常没有 options, 也要认得出来"""
    card = {
        "jobid": "4",
        "type": "video",
        "property": {
            "name": "v.mp4",
            "quiz": [{"id": "j1", "title": "政治安全是国家安全根本。", "type": "judgement"}],
        },
    }
    questions = find_inline_questions(card)
    assert len(questions) == 1
    assert questions[0]["type"] == "judgement"
    print("[ok] 无选项的判断题也能识别")


# ---------------------------------------------------------------- 作答与提交
def _quiz_payload(**overrides):
    """模拟 initdatawithviewerV2 的真实响应结构"""
    question = {
        "startTime": 120,
        "validationUrl": "/mooc-ans/question/quiz-validation?xx=1",
        "resourceId": "res-1",
        "description": "政治安全的核心是什么?",
        "type": "single",
        "options": [
            {"name": "A", "description": "政权安全"},
            {"name": "B", "description": "经济安全"},
        ],
    }
    question.update(overrides)
    return [{"datas": [question]}]


def test_parse_quiz_list():
    quizzes = parse_quiz_list(_quiz_payload())
    assert len(quizzes) == 1, quizzes
    q = quizzes[0]
    assert q["time"] == 120.0
    assert q["resourceId"] == "res-1"
    assert q["validationUrl"].startswith("/mooc-ans/")
    assert "A.政权安全" in q["options"], q["options"]
    assert q["optionList"][0]["name"] == "A"
    print("[ok] 能从 initdata 响应解析出弹窗题(含提交地址)")


def test_parse_quiz_list_skips_unsubmittable():
    """没有 validationUrl 的条目(讲义之类)不该进列表 —— 做了也提交不了"""
    assert parse_quiz_list(_quiz_payload(validationUrl="")) == []
    print("[ok] 没有提交地址的条目不当作弹窗题")


def test_option_name_for_letter():
    quiz = {"optionList": [{"name": "opt-1", "description": "甲"},
                           {"name": "opt-2", "description": "乙"}]}
    assert option_name_for_letter(quiz, "B") == "opt-2", "要提交服务端的 name, 不是字母"
    assert option_name_for_letter(quiz, "Z") == "Z"
    print("[ok] 字母能映射成服务端认的选项 name")


def test_due_quiz_submitted_at_pop_time():
    """进度到弹题时间才提交, 提前不动作"""
    class FakeEngine:
        def solve_all(self, questions, title=""):
            for item in questions:
                qid = item.get("id") or ""
                item.setdefault("answerField", {})[f"answer{qid}"] = "A"

            class Report:
                def summary(self):
                    return "假报告"

            return Report()

    class FakeSession:
        def __init__(self):
            self.calls = []

        def get(self, url, **kwargs):
            self.calls.append((url, kwargs.get("params")))

            class Resp:
                status_code = 200
                text = '{"status":true}'

            return Resp()

    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.answer_engine = FakeEngine()
    chaoxing.inline_quiz_stat = {"total": 1, "answered": 0, "submitted": 0, "failed": 0}
    chaoxing._inline_lock = threading.Lock()
    chaoxing._inline_seen = set()

    quizzes = parse_quiz_list(_quiz_payload())
    session = FakeSession()

    # 还没播到 120 秒: 不提交
    chaoxing._submit_due_inline_quizzes(session, quizzes, 30, {})
    assert session.calls == [], session.calls

    # 越过弹题时间: 作答 + 提交
    chaoxing._submit_due_inline_quizzes(session, quizzes, 121, {})
    assert len(session.calls) == 1, session.calls
    url, params = session.calls[0]
    assert url.startswith("https://mooc1.chaoxing.com/mooc-ans/"), url
    assert params == {"resourceid": "res-1", "answer": "A"}, params
    assert chaoxing.inline_quiz_stat["submitted"] == 1

    # 同一道题只提交一次
    chaoxing._submit_due_inline_quizzes(session, quizzes, 200, {})
    assert len(session.calls) == 1, "重复提交同一道题"
    print("[ok] 播到弹题时间才作答并提交, 且只提交一次")


if __name__ == "__main__":
    test_find_questions_in_card()
    test_no_false_positive_on_plain_card()
    test_detect_face_capture()
    test_face_capture_needs_real_evidence()
    test_face_capture_with_nested_property()
    test_question_dedup()
    test_judgement_without_options()
    test_parse_quiz_list()
    test_parse_quiz_list_skips_unsubmittable()
    test_option_name_for_letter()
    test_due_quiz_submitted_at_pop_time()
    print("\n全部通过")
