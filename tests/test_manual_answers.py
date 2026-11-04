# -*- coding: utf-8 -*-
"""人工校订答案(manual_answers.json)测试

要钉住的行为:
  1. 题干比对要够宽容 —— 题号、题型标记、空格、中英文标点、分数后缀都不影响;
  2. 但也不能乱认 —— 不相干的题不能被"相似度"兜进来, 判断题的 A/B 不当成对错;
  3. 命中后优先级最高 —— 不再问题库, 也不再问大模型(省钱);
  4. 命中算"有依据的答案", 覆盖率要把它算进去, 否则会因为覆盖率不够不提交;
  5. 文件改了要能自动重载(本地边跑边改), 文件不存在要静默跳过。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.ai_solver import AIAnswer  # noqa: E402
from api.answer_engine import AnswerEngine  # noqa: E402
from api.manual_answers import (  # noqa: E402
    ManualAnswerBook,
    configure_default_answer_book,
    normalize_title,
)

TMP_DIR = tempfile.mkdtemp(prefix="manual_answers_")


def write_book(entries, name="manual_answers.json"):
    path = os.path.join(TMP_DIR, name)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({"version": 1, "entries": entries}, fp, ensure_ascii=False)
    return path


def make_question(**kwargs):
    question = {
        "id": "q1",
        "title": "1【判断题】经济安全事关国家的国际地位。（）（1.0分）",
        "options": "",
        "type": "judgement",
        "answerField": {"answerq1": "", "answertypeq1": "3"},
    }
    question.update(kwargs)
    return question


class FakeSolver:
    """假的大模型: 只回答 A, 并且数一下被叫了几次"""

    available = True

    def __init__(self):
        self.calls = 0

    def solve(self, question):
        self.calls += 1
        return AIAnswer(answer=["A"], confidence=0.9, reason="fake")

    def solve_many(self, questions):
        self.calls += 1
        return {q.get("id", ""): AIAnswer(answer=["A"], confidence=0.9) for q in questions}


# ---------------------------------------------------------------- 归一化
def test_normalize_ignores_noise():
    """题号 / 题型标记 / 分数 / 空格 / 标点都不该影响比对"""
    variants = [
        "1【判断题】经济安全事关国家的国际地位。（）（1.0分）",
        "经济安全事关国家的国际地位",
        "  经济安全 事关国家的国际地位  ",
        "判断题：经济安全事关国家的国际地位！",
        "2[单选题]经济安全，事关国家的国际地位",
    ]
    keys = {normalize_title(text) for text in variants}
    assert len(keys) == 1, keys
    print("[ok] 归一化: 题号/题型/分数/空格/标点都不影响比对")


def test_normalize_keeps_leading_year():
    """题干本身以数字开头(1949年…)时, 不能把年份当题号吃掉"""
    key = normalize_title("1949年新中国成立")
    assert key.startswith("1949"), key
    print("[ok] 归一化: 题干开头的年份没被当成题号删掉")


# ---------------------------------------------------------------- 匹配
def test_lookup_exact_and_partial():
    path = write_book([
        {"title": "经济安全事关国家的国际地位。", "type": "judgement", "answer": "错误"},
        {"title": "下列属于国家安全体系组成部分的是", "type": "multiple", "answer": "ABD"},
    ])
    book = ManualAnswerBook(path)
    assert len(book) == 2

    hit = book.lookup(make_question())
    assert hit and hit["answer"] == "错误", hit

    # 只写题干开头一段(>= 8 字)也要能命中
    partial = book.lookup(make_question(title="下列属于国家安全体系组成部分的是以下哪些选项?"))
    assert partial and partial["answer"] == "ABD", partial
    print("[ok] 匹配: 完全一致 与「只写题干片段」都能命中")


def test_lookup_similarity_fallback():
    """题干有个错别字/多几个字, 相似度兜底要接住"""
    path = write_book([
        {"title": "经济安全事关国家的国际地位和影响力", "type": "judgement", "answer": "错误"},
    ])
    book = ManualAnswerBook(path)
    hit = book.lookup(make_question(title="1【判断题】经济安全事关国家的国际地位和影响力。（）"))
    assert hit is not None, "相似题干没命中"
    print("[ok] 匹配: 题干有微调时按相似度兜底命中")


def test_lookup_does_not_match_unrelated():
    """不相干的题绝对不能命中 —— 错用一条人工答案比没有更糟"""
    path = write_book([
        {"title": "经济安全事关国家的国际地位", "type": "judgement", "answer": "错误"},
    ])
    book = ManualAnswerBook(path)
    assert book.lookup(make_question(title="政治安全的核心是政权安全")) is None
    print("[ok] 匹配: 不相干的题不会误命中")


def test_disabled_entry_ignored():
    """模板里的示例条目(disabled)只给人看, 不参与匹配"""
    path = write_book([
        {"title": "经济安全事关国家的国际地位", "type": "judgement", "answer": "错误", "disabled": True},
    ])
    book = ManualAnswerBook(path)
    assert len(book) == 0
    assert book.lookup(make_question()) is None
    print("[ok] disabled 的示例条目不生效")


def test_missing_file_is_silent():
    """没有这个文件是正常情况(不配也能跑)"""
    book = ManualAnswerBook(os.path.join(TMP_DIR, "not_exists.json"))
    assert len(book) == 0
    assert book.lookup(make_question()) is None
    print("[ok] 文件不存在时不报错、不命中")


def test_reload_on_change():
    """文件被改过要自动重载, 本地可以边跑边改"""
    path = write_book([{"title": "经济安全事关国家的国际地位", "type": "judgement", "answer": "错误"}])
    book = ManualAnswerBook(path)
    assert book.lookup(make_question())["answer"] == "错误"

    write_book([{"title": "经济安全事关国家的国际地位", "type": "judgement", "answer": "正确"}])
    time.sleep(2.1)  # 重载有 2 秒节流
    hit = book.lookup(make_question())
    assert hit and hit["answer"] == "正确", hit
    print("[ok] 文件改动后自动重载")


# ---------------------------------------------------------------- 答案格式
def build_engine(solver=None):
    return AnswerEngine(None, ai_solver=solver, ai_mode="fallback" if solver else "off")


def solve_with(entries, question, solver=None):
    configure_default_answer_book(write_book(entries))
    engine = build_engine(solver)
    return engine.solve_one(question)


def test_judgement_answer_formats():
    """判断题: 中文/符号/英文写法都要认"""
    for text, expect in [
        ("错误", "false"), ("错", "false"), ("×", "false"), ("false", "false"), ("F", "false"),
        ("正确", "true"), ("对", "true"), ("√", "true"), ("true", "true"), ("T", "true"),
    ]:
        solved = solve_with(
            [{"title": "经济安全事关国家的国际地位", "answer": text}], make_question()
        )
        assert solved.answer == expect, f"{text} -> {solved.answer}, 期望 {expect}"
        assert solved.source == "manual", solved.source
    print("[ok] 判断题: 正确/错误、对/错、√/×、true/false 都能解析")


def test_judgement_rejects_letters():
    """判断题写 A 不认 —— 有的课 A 是「正确」有的课不是, 猜错了就是错答"""
    solved = solve_with(
        [{"title": "经济安全事关国家的国际地位", "answer": "A"}], make_question()
    )
    assert solved.source != "manual", "判断题不该把 A 当成对错"
    print("[ok] 判断题: 不把 A/B 当成正确/错误(避免瞎猜)")


def test_choice_letters_and_text():
    """选择题: 写字母 / 写选项原文, 都要能转成提交格式"""
    single = make_question(
        title="2【单选题】国家安全的基础是（）", type="single",
        options="A.经济安全\nB.军事安全\nC.文化安全\nD.科技安全",
        answerField={"answerq1": "", "answertypeq1": "0"},
    )
    solved = solve_with([{"title": "国家安全的基础是", "answer": "a"}], single)
    assert solved.answer == "A", solved.answer

    solved = solve_with([{"title": "国家安全的基础是", "answer": "经济安全"}], single)
    assert solved.answer == "A", solved.answer

    multiple = make_question(
        title="3【多选题】国家安全体系包括（）", type="multiple",
        options="A.经济安全\nB.军事安全\nC.文化安全\nD.外星安全",
        answerField={"answerq1": "", "answertypeq1": "1"},
    )
    # 乱序写也要排好序, 多了逗号也不怕
    solved = solve_with([{"title": "国家安全体系包括", "answer": "D,A,B"}], multiple)
    assert solved.answer == "ABD", solved.answer
    print("[ok] 选择题: 字母(乱序/小写/带逗号)与选项原文都能转成提交格式")


def test_completion_multi_blank():
    """填空题: 多个空用换行分隔"""
    question = make_question(
        title="4【填空题】____和____是国家安全的两大支柱", type="completion",
        options="", answerField={"answerq1": "", "answertypeq1": "2"},
    )
    solved = solve_with([{"title": "和是国家安全的两大支柱", "answer": "经济|军事"}], question)
    assert solved.answer == "经济\n军事", repr(solved.answer)
    print("[ok] 填空题: 多空答案转换正确")


# ---------------------------------------------------------------- 优先级
def test_manual_beats_ai_and_tiku():
    """命中人工答案后: 不再问题库, 也不再问大模型"""
    class LoudTiku:
        DISABLE = False

        def __init__(self):
            self.calls = 0

        def query(self, info):
            self.calls += 1
            return "正确"

    tiku = LoudTiku()
    solver = FakeSolver()
    configure_default_answer_book(write_book([
        {"title": "经济安全事关国家的国际地位", "answer": "错误"},
    ]))
    engine = AnswerEngine(tiku, ai_solver=solver, ai_mode="fallback")
    solved = engine.solve_one(make_question())

    assert solved.source == "manual", solved.source
    assert solved.answer == "false", solved.answer
    assert tiku.calls == 0, "命中人工答案后不该再问题库"
    assert solver.calls == 0, "命中人工答案后不该再问大模型"
    print("[ok] 优先级: 人工答案压过题库与大模型, 两者都没被调用")


def test_manual_counts_as_covered():
    """命中人工答案要算「有依据」, 否则覆盖率不够就不提交了"""
    configure_default_answer_book(write_book([
        {"title": "经济安全事关国家的国际地位", "answer": "错误"},
    ]))
    engine = build_engine()
    report = engine.solve_all([make_question()], title="章节测验")
    assert report.manual_count == 1, report.manual_count
    assert report.covered == 1, report.covered
    assert report.cover_rate == 1.0, report.cover_rate
    print("[ok] 覆盖率: 人工命中计入「有依据的答案」")


def test_priority_mode_skips_ai_for_manual_questions():
    """AI 优先模式下, 已有人工答案的题不送大模型(省钱)"""
    configure_default_answer_book(write_book([
        {"title": "经济安全事关国家的国际地位", "answer": "错误"},
    ]))
    solver = FakeSolver()
    solved_q = make_question()
    open_q = make_question(id="q2", title="5【单选题】完全没人答过的题（）",
                           type="single", options="A.甲\nB.乙")
    engine = AnswerEngine(None, ai_solver=solver, ai_mode="priority")
    report = engine.solve_all([solved_q, open_q], title="章节测验")

    assert solver.calls == 1, "应该只对没有人工答案的那道题问模型"
    assert report.manual_count == 1
    assert report.ai_count == 1
    print("[ok] AI 优先模式: 已有人工答案的题不重复送模型")


def test_no_book_no_effect():
    """没配人工答案时, 行为与以前完全一致(题库/大模型各自照常)"""
    book = configure_default_answer_book(os.path.join(TMP_DIR, "absent.json"))
    assert book is not None and len(book) == 0
    solver = FakeSolver()
    engine = AnswerEngine(None, ai_solver=solver, ai_mode="fallback")
    question = make_question(id="q9", title="6【单选题】没配人工答案的题（）", type="single",
                             options="A.甲\nB.乙")
    solved = engine.solve_one(question)
    assert solved.source == "ai", solved.source
    assert solver.calls == 1
    print("[ok] 没配人工答案时, 一切照旧")


def test_manual_alone_counts_as_source():
    """只填了 manual_answers.json、既没题库也没 AI 时, 也该进入作答而不是直接跳过"""
    configure_default_answer_book(write_book([
        {"title": "经济安全事关国家的国际地位", "answer": "错误"},
    ]))
    engine = AnswerEngine(None, ai_solver=None, ai_mode="off")
    assert engine.has_answer_source() is True, "有人工答案却没被当成答案来源"

    configure_default_answer_book(os.path.join(TMP_DIR, "absent2.json"))
    bare = AnswerEngine(None, ai_solver=None, ai_mode="off")
    assert bare.has_answer_source() is False
    print("[ok] 答案来源: 只有人工答案时也算有来源, 不会跳过作答")


if __name__ == "__main__":
    test_normalize_ignores_noise()
    test_normalize_keeps_leading_year()
    test_lookup_exact_and_partial()
    test_lookup_similarity_fallback()
    test_lookup_does_not_match_unrelated()
    test_disabled_entry_ignored()
    test_missing_file_is_silent()
    test_reload_on_change()
    test_judgement_answer_formats()
    test_judgement_rejects_letters()
    test_choice_letters_and_text()
    test_completion_multi_blank()
    test_manual_beats_ai_and_tiku()
    test_manual_counts_as_covered()
    test_priority_mode_skips_ai_for_manual_questions()
    test_no_book_no_effect()
    test_manual_alone_counts_as_source()
    print("\n全部通过")
