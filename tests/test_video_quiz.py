# -*- coding: utf-8 -*-
"""视频内嵌题(弹窗题)识别测试

背景: 每小节视频播放中途可能弹出小题, 不答视频就停住, 服务端也不会把
任务点判为通过 —— 表现是「进度到片尾了, isPassed 依然是 false」。
题目数据跟着章节卡片一起下发, 但字段名不稳定, 所以用结构化扫描。
这里把「认得出来」和「不乱认」两件事都钉住。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.video_quiz import find_inline_questions, summarize_video_card  # noqa: E402


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


if __name__ == "__main__":
    test_find_questions_in_card()
    test_no_false_positive_on_plain_card()
    test_detect_face_capture()
    test_question_dedup()
    test_judgement_without_options()
    print("\n全部通过")
