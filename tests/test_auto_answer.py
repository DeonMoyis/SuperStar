# -*- coding: utf-8 -*-
"""
作业 / 考试 自动答题 离线自测

不需要登录、不联网, 用假的 HTTP 响应验证:
1. 列表页解析 (作业 / 考试, href 与 onclick 两种写法)
2. 题目作答 (单选 / 多选 / 判断 / 填空 / 随机兜底)
3. 覆盖率统计与提交策略 (auto 达标提交 / 不达标只保存)
4. 提交表单拼装

运行方式 (在仓库根目录):
    python tests/test_auto_answer.py
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.answer_engine import AnswerEngine, build_submit_payload
from api.auto_answer import AutoAnswer, decode_task_list, normalize_answer_config
from api.base import Chaoxing, SessionManager

WORK_LIST_HTML = """
<html><body><ul>
<li class="work-item"><a href="/mooc-ans/work/doHomeWorkNew?courseId=111&classId=222&workId=333&answerId=444&enc=abc&cpi=555&ut=s">第一章 作业</a><span>未交</span></li>
<li class="work-item"><a href="javascript:;" onclick="window.open('/mooc-ans/work/doHomeWorkNew?courseId=111&classId=222&workId=666&enc=def&cpi=555&ut=s')">第二章 作业</a><span>已完成</span></li>
</ul></body></html>
"""

EXAM_LIST_HTML = """
<html><body><ul>
<li class="exam-item"><a href="/mooc-ans/exam/test/reVersionTestStartNew?courseId=1&classId=2&examId=3">期末考试</a><span>未开始</span></li>
<li class="exam-item"><a href="/mooc-ans/exam/test/reVersionTestStartNew?courseId=1&classId=2&examId=4">期中考试</a><span>已批阅</span></li>
</ul></body></html>
"""

ANSWER_PAGE_HTML = """
<html><body>
<form action="/mooc-ans/work/addStudentWorkNew" method="post">
<input type="hidden" name="courseId" value="111">
<input type="hidden" name="classId" value="222">
<input type="hidden" name="workId" value="333">
<input type="hidden" name="pyFlag" value="">
<div class="singleQuesId" data="9001">
  <div class="TiMu" data="0">
    <div class="Zy_TItle"><span>1+1=?（2.0分）</span></div>
    <ul>
      <li aria-label="A.1">A.1</li>
      <li aria-label="B.2">B.2</li>
      <li aria-label="C.3">C.3</li>
    </ul>
  </div>
</div>
<div class="singleQuesId" data="9002">
  <div class="TiMu" data="1">
    <div class="Zy_TItle"><span>下列哪些是偶数（3.0分）</span></div>
    <ul>
      <li aria-label="A.1">A.1</li>
      <li aria-label="B.2">B.2</li>
      <li aria-label="C.3">C.3</li>
      <li aria-label="D.4">D.4</li>
    </ul>
  </div>
</div>
</form>
</body></html>
"""


class FakeResponse:
    def __init__(self, text="", status_code=200, url="", payload=None):
        self.text = text
        self.status_code = status_code
        self.url = url
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class FakeSession:
    def __init__(self):
        self.posted = []

    def get(self, url, **kwargs):
        if "work/list" in url:
            return FakeResponse(WORK_LIST_HTML, url=url)
        if "exam-list" in url:
            return FakeResponse(EXAM_LIST_HTML, url=url)
        if "doHomeWorkNew" in url or "reVersionTestStartNew" in url:
            html = ANSWER_PAGE_HTML
            if "reVersionTestStartNew" in url:
                # 考试页的提交地址和作业不同, 用来验证「表单 action 优先」这条逻辑
                html = html.replace(
                    "/mooc-ans/work/addStudentWorkNew",
                    "/mooc-ans/exam/test/addExamTranExamNew",
                )
            return FakeResponse(html, url=url)
        return FakeResponse("<html></html>", url=url)

    def post(self, url, data=None, headers=None, **kwargs):
        self.posted.append((url, data))
        return FakeResponse(payload={"status": True, "msg": "提交成功"})


class FakeTiku:
    """模拟真实题库, 包括它会把「（2.0分）」后缀去掉再查的行为"""
    DISABLE = False

    def __init__(self, answers=None):
        self.answers = answers if answers is not None else {
            "1+1=?": "2",
            "下列哪些是偶数": "2,4",
            "地球是圆的": "正确",
            "水的化学式": "H2O",
        }

    def query(self, q_info):
        title = re.sub(r"（\d+\.\d+分）$", "", q_info["title"])
        return self.answers.get(title)

    def judgement_select(self, answer):
        return str(answer).strip() in {"正确", "对", "√", "是"}


def make_question(qid, title, qtype, options="", type_code="0"):
    return {
        "id": qid,
        "title": title,
        "options": options,
        "type": qtype,
        "answerField": {f"answer{qid}": "", f"answertype{qid}": type_code},
    }


def test_decode_task_list():
    works = decode_task_list(WORK_LIST_HTML, "work")
    assert len(works) == 2, f"作业条数不对: {len(works)}"
    assert works[0].finished is False, "第一章应判为未交"
    assert works[1].finished is True, "第二章应判为已完成"
    assert works[1].url.endswith("workId=666&enc=def&cpi=555&ut=s"), works[1].url
    assert works[0].url.startswith("https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew")

    exams = decode_task_list(EXAM_LIST_HTML, "exam")
    assert len(exams) == 2, f"考试条数不对: {len(exams)}"
    assert exams[0].finished is False and exams[1].finished is True
    print("[ok] 列表解析:", [(t.title, t.status) for t in works + exams])


def test_answer_engine():
    engine = AnswerEngine(FakeTiku())
    questions = [
        make_question("1", "1+1=?", "single", "A.1\nB.2\nC.3"),
        make_question("2", "下列哪些是偶数", "multiple", "A.1\nB.2\nC.3\nD.4", "1"),
        make_question("3", "地球是圆的", "judgement", "A.对\nB.错", "3"),
        make_question("4", "水的化学式", "completion", "", "2"),
        make_question("5", "题库里没有这道题", "single", "A.甲\nB.乙\nC.丙"),
    ]
    report = engine.solve_all(questions, title="自测")
    answers = [item.answer for item in report.solved]
    assert answers[0] == "B", answers[0]
    assert answers[1] == "BD", answers[1]
    assert answers[2] == "true", answers[2]
    assert answers[3] == "H2O", answers[3]
    assert answers[4] in {"A", "B", "C"}, answers[4]
    assert report.covered == 4 and report.total == 5
    assert abs(report.cover_rate - 0.8) < 1e-6
    print("[ok] 作答结果:", answers, "| 覆盖率:", f"{report.cover_rate * 100:.0f}%")

    form = {"pyFlag": "1", "answerwqbid": "1,2,", "courseId": "111", "questions": questions}
    saved = build_submit_payload(form, report)
    assert saved["answer5"] == "", "保存模式下未命中题目的答案应留空"
    form["pyFlag"] = ""
    submitted = build_submit_payload(form, report)
    assert submitted["answer5"] in {"A", "B", "C"}, "提交模式下随机答案应填入"
    assert submitted["answer1"] == "B" and submitted["answertype1"] == "0"
    print("[ok] 表单拼装: 保存 / 提交两种模式都正确")


def test_pipeline():
    fake_session = FakeSession()
    original = SessionManager.get_session
    SessionManager.get_session = classmethod(lambda cls: fake_session)
    try:
        chaoxing = Chaoxing(account=None, tiku=FakeTiku())
        course = {"courseId": "111", "clazzId": "222", "cpi": "555", "title": "测试课程"}

        # 场景1: 覆盖率达标 -> 自动提交
        answer = AutoAnswer(chaoxing, {"enable": True, "submit_policy": "auto",
                                       "min_cover_rate": 0.5, "exam": True})
        stats = answer.run_course(course)
        assert stats.tasks_found == 4, stats.tasks_found          # 2 份作业 + 2 份考试
        assert stats.answered == 2, f"应只作答未完成的那 2 份, 实际 {stats.answered}"
        assert stats.submitted == 2, "覆盖率达标应全部提交"
        # 默认 exam_priority=true, 考试排在作业前面
        exam_url, _ = fake_session.posted[0]
        work_url, data = fake_session.posted[1]
        assert work_url.endswith("/mooc-ans/work/addStudentWorkNew"), work_url
        assert exam_url.endswith("/mooc-ans/exam/test/addExamTranExamNew"), exam_url
        assert data["answer9001"] == "B" and data["answer9002"] == "BD"
        assert data["pyFlag"] == "", "达标应正式提交"
        print("[ok] 达标场景:", stats.summary())

        # 场景2: 覆盖率不达标 -> 只保存
        fake_session.posted.clear()
        empty_tiku = FakeTiku(answers={})
        low = AutoAnswer(chaoxing, {"enable": True, "submit_policy": "auto",
                                    "min_cover_rate": 1.01, "exam": True})
        low.engine.tiku = empty_tiku
        stats_low = low.run_course(course)
        assert stats_low.saved == 2 and stats_low.submitted == 0
        _, data_low = fake_session.posted[0]
        assert data_low["pyFlag"] == "1", "未达标应保存草稿"
        assert data_low["answer9001"] == "", "保存模式下随机答案应留空"
        print("[ok] 未达标场景:", stats_low.summary())

        # 场景3: never 策略 -> 无论覆盖率都只保存
        fake_session.posted.clear()
        never = AutoAnswer(chaoxing, {"enable": True, "submit_policy": "never", "exam": True})
        stats_never = never.run_course(course)
        assert stats_never.submitted == 0 and stats_never.saved == 2
        print("[ok] 只保存策略:", stats_never.summary())

        # 场景4: 默认不代考 -> 只扫到作业, 考试完全不碰
        fake_session.posted.clear()
        work_only = AutoAnswer(chaoxing, {"enable": True, "submit_policy": "auto"})
        stats_work = work_only.run_course(course)
        assert stats_work.tasks_found == 2, f"默认应只发现 2 份作业, 实际 {stats_work.tasks_found}"
        kinds = ["考试" if "exam" in url else "作业" for url, _ in fake_session.posted]
        assert all(k == "作业" for k in kinds), f"默认不该提交考试, 实际 {kinds}"
        print("[ok] 默认不代考:", stats_work.summary())
    finally:
        SessionManager.get_session = original


def test_config():
    config = normalize_answer_config({"enable": "true", "min_cover_rate": "0.8", "submit_policy": "weird"})
    assert config["enable"] is True
    assert config["min_cover_rate"] == 0.8
    assert config["submit_policy"] == "auto", "非法策略应回退为 auto"
    assert normalize_answer_config(None)["enable"] is False
    # 考试相关的两个新配置
    assert normalize_answer_config(None)["exam"] is False, "默认不代考(考试留给用户自己考)"
    assert normalize_answer_config(None)["exam_priority"] is True, "默认应考试优先"
    assert normalize_answer_config(None)["exam_min_remaining"] == 15
    custom = normalize_answer_config({"exam_priority": "false", "exam_min_remaining": "30"})
    assert custom["exam_priority"] is False and custom["exam_min_remaining"] == 30
    bad = normalize_answer_config({"exam_min_remaining": "abc"})
    assert bad["exam_min_remaining"] == 15, "非法值应回退为默认 15 分钟"
    print("[ok] 配置归一化:", config)


def test_exam_priority_order():
    """考试必须排在作业前面, 否则时间盒到点时考试会被饿死"""
    fake_session = FakeSession()
    original = SessionManager.get_session
    SessionManager.get_session = classmethod(lambda cls: fake_session)
    try:
        chaoxing = Chaoxing(account=None, tiku=FakeTiku())
        course = {"courseId": "111", "clazzId": "222", "cpi": "555", "title": "排序课程"}

        answer = AutoAnswer(chaoxing, {"enable": True, "submit_policy": "always", "exam": True})
        answer.run_course(course)
        kinds = ["考试" if "exam" in url else "作业" for url, _ in fake_session.posted]
        assert kinds == ["考试", "作业"], f"应考试在前, 实际 {kinds}"
        print("[ok] 考试优先排序:", kinds)

        # 关掉优先时应恢复列表顺序(作业在前)
        fake_session.posted.clear()
        normal = AutoAnswer(
            chaoxing,
            {"enable": True, "submit_policy": "always", "exam": True, "exam_priority": "false"},
        )
        normal.run_course(course)
        kinds = ["考试" if "exam" in url else "作业" for url, _ in fake_session.posted]
        assert kinds == ["作业", "考试"], f"关闭优先后应作业在前, 实际 {kinds}"
        print("[ok] 关闭考试优先:", kinds)
    finally:
        SessionManager.get_session = original


def test_exam_start_blocked_by_remaining_time():
    """剩余时间不足阈值时不开考, 作业照做"""
    from api.runtime_budget import runtime_budget

    fake_session = FakeSession()
    original = SessionManager.get_session
    SessionManager.get_session = classmethod(lambda cls: fake_session)
    original_budget = runtime_budget._limit
    try:
        # 上限 5 分钟, 开考门槛 15 分钟 -> 剩余不够, 考试应被推迟
        runtime_budget.configure(5)
        chaoxing = Chaoxing(account=None, tiku=FakeTiku())
        course = {"courseId": "111", "clazzId": "222", "cpi": "555", "title": "时间不足"}

        answer = AutoAnswer(
            chaoxing,
            {"enable": True, "submit_policy": "always", "exam": True, "exam_min_remaining": 15},
        )
        stats = answer.run_course(course)
        kinds = ["考试" if "exam" in url else "作业" for url, _ in fake_session.posted]
        assert kinds == ["作业"], f"考试应被推迟, 实际提交了 {kinds}"
        assert stats.postponed == 1, stats.postponed
        # skipped 只统计「已完成/已批阅」的 2 份, 推迟的考试不算跳过
        assert stats.answered == 1 and stats.skipped == 2, (stats.answered, stats.skipped)
        assert "推迟" in stats.summary(), stats.summary()
        print("[ok] 剩余时间不足不开考:", stats.summary())

        # 门槛设为 0 时应关闭检查, 照常开考
        fake_session.posted.clear()
        runtime_budget.configure(5)
        eager = AutoAnswer(
            chaoxing,
            {"enable": True, "submit_policy": "always", "exam": True, "exam_min_remaining": 0},
        )
        stats_eager = eager.run_course(course)
        assert stats_eager.postponed == 0 and stats_eager.answered == 2
        print("[ok] 关闭检查后正常开考:", stats_eager.summary())
    finally:
        runtime_budget.configure(original_budget / 60 if original_budget else None)
        SessionManager.get_session = original


def test_exam_switch():
    """--no-exam / --exam 必须能覆盖配置文件里的 exam"""
    import sys as _sys

    from main import build_config_from_args, parse_args

    def build(argv):
        original = _sys.argv
        _sys.argv = ["main.py"] + argv
        try:
            return build_config_from_args(parse_args())[0]["answer"]
        finally:
            _sys.argv = original

    base = ["-u", "a", "-p", "b", "--auto-answer"]
    assert "exam" not in build(base), "没显式指定时不该覆盖配置文件"

    off = build(base + ["--no-exam"])
    assert off["exam"] == "false", off
    assert normalize_answer_config(off)["exam"] is False
    assert normalize_answer_config(off)["work"] is True, "关考试不该影响作业"

    on = build(base + ["--exam"])
    assert on["exam"] == "true", on
    assert normalize_answer_config(on)["exam"] is True
    print("[ok] 考试开关: 默认不覆盖 / --no-exam / --exam")


def main():
    test_decode_task_list()
    test_answer_engine()
    test_config()
    test_exam_switch()
    test_exam_priority_order()
    test_exam_start_blocked_by_remaining_time()
    test_pipeline()
    print("\n全部离线自测通过")


if __name__ == "__main__":
    main()
