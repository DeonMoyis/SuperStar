# -*- coding: utf-8 -*-
"""章节测验(每小节那份作业)的作答与提交策略测试

背景: 旧实现里章节测验只认题库, 没配 [tiku] 就原地 return SUCCESS,
日志里只有一句"未找到tiku配置", 于是「每小节一份作业」一份都不会做。
现在改成 题库 -> 大模型 -> 随机 的降级链, 这里把门槛和提交策略钉死。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.base as base  # noqa: E402
from api.answer_engine import AnswerEngine  # noqa: E402


class DisabledTiku:
    """没配 [tiku] 时的题库状态"""

    DISABLE = True

    def query(self, question):
        return None


class HealthyTiku:
    DISABLE = False

    def query(self, question):
        return "A"


class FakeAISolver:
    available = True


def make_chaoxing(tiku, ai_solver=None, policy=None):
    """伪造 Chaoxing 实例, 只关心章节测验的门槛与提交策略"""
    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.tiku = tiku
    chaoxing.rollback_times = 0
    chaoxing.answer_policy = policy or {}
    chaoxing.answer_engine = AnswerEngine(tiku, ai_solver=ai_solver)

    touched = {"count": 0}

    def fake_locked(_course, _job, _job_info):
        touched["count"] += 1
        return base.StudyResult.SUCCESS

    chaoxing._study_work_locked = fake_locked
    return chaoxing, touched


# ---------------------------------------------------------------- 作答门槛
def test_ai_alone_is_enough():
    """没题库但有 AI: 章节测验必须真的去作答, 不能静默跳过"""
    chaoxing, touched = make_chaoxing(DisabledTiku(), ai_solver=FakeAISolver())
    result = chaoxing.study_work({"courseId": "1"}, {"name": "小节测验"}, {})

    assert result == base.StudyResult.SUCCESS, result
    assert touched["count"] == 1, "有 AI 却没有进入作答, 说明门槛还卡在题库上"
    print("[ok] 无题库 + 有 AI: 正常进入作答")


def test_tiku_alone_is_enough():
    """有题库没 AI: 保持老行为, 照常作答"""
    chaoxing, touched = make_chaoxing(HealthyTiku())
    chaoxing.study_work({"courseId": "1"}, {"name": "小节测验"}, {})
    assert touched["count"] == 1
    print("[ok] 有题库: 正常进入作答")


def test_nothing_available_skips():
    """题库和 AI 都没有: 跳过, 不浪费时间去答一串随机答案"""
    chaoxing, touched = make_chaoxing(DisabledTiku())
    assert chaoxing._answer_available() is False
    result = chaoxing.study_work({"courseId": "1"}, {"name": "小节测验"}, {})
    assert result == base.StudyResult.SUCCESS
    assert touched["count"] == 0, "没有任何答案来源时不该进入作答"
    print("[ok] 题库/AI 都没有: 跳过作答")


# ---------------------------------------------------------------- 提交策略
def test_submit_policy_always():
    chaoxing, _ = make_chaoxing(HealthyTiku(), policy={"submit_policy": "always"})
    assert chaoxing._should_submit(0) is True, "always 策略下零覆盖率也提交"
    print("[ok] submit_policy=always: 总是提交")


def test_submit_policy_never():
    chaoxing, _ = make_chaoxing(HealthyTiku(), policy={"submit_policy": "never"})
    assert chaoxing._should_submit(100) is False, "never 策略下满分也只保存"
    print("[ok] submit_policy=never: 只保存")


def test_submit_policy_auto_threshold():
    chaoxing, _ = make_chaoxing(
        HealthyTiku(), policy={"submit_policy": "auto", "min_cover_rate": 0.9}
    )
    assert chaoxing._should_submit(95) is True
    assert chaoxing._should_submit(89) is False, "覆盖率低于阈值应只保存"
    print("[ok] submit_policy=auto: 90% 阈值生效")


def test_submit_policy_default_is_auto():
    """完全没有 [answer] 配置时, 默认按 auto 处理, 不会静默变成"只保存" """
    chaoxing, _ = make_chaoxing(HealthyTiku(), policy={})
    assert chaoxing._should_submit(100) is True
    assert chaoxing._should_submit(10) is False
    print("[ok] 默认策略: auto")


def test_rollback_forces_submit():
    """答过一轮被判定不合格会回滚一次, 回滚后不再卡阈值"""
    chaoxing, _ = make_chaoxing(HealthyTiku(), policy={"submit_policy": "auto"})
    chaoxing.rollback_times = 1
    assert chaoxing._should_submit(10) is True
    print("[ok] 回滚一次后强制提交")


if __name__ == "__main__":
    test_ai_alone_is_enough()
    test_tiku_alone_is_enough()
    test_nothing_available_skips()
    test_submit_policy_always()
    test_submit_policy_never()
    test_submit_policy_auto_threshold()
    test_submit_policy_default_is_auto()
    test_rollback_forces_submit()
    print("\n全部通过")
