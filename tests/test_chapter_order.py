# -*- coding: utf-8 -*-
"""章节内任务点的执行顺序测试

背景: 旧实现用 ThreadPoolExecutor(5) 把整章任务点一起塞进线程池,
视频在排队等播放位, 章节测验却可能先抢到作答位先跑, 表现为
「这一节的视频还没看, 测验已经交了」。超星的章节测验普遍要求先学完
本节内容才允许作答, 抢在前面提交容易判未完成。

这里把两件事钉死:
  1. 章节内一定是「先把视频/文档学完, 再做本节测验」;
  2. 内容还没学完就到运行时长上限时, 本节测验不再发起请求。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main as m  # noqa: E402
from api.base import StudyResult  # noqa: E402


class FakeRateLimiter:
    def limit_rate(self, *args, **kwargs):
        return None


class FakeChaoxing:
    """只提供 process_chapter 需要的那一个接口"""

    def __init__(self, jobs):
        self.jobs = jobs
        self.rate_limiter = FakeRateLimiter()

    def get_job_list(self, course, point):
        return list(self.jobs), {}


class FakeBudget:
    """第 stop_after 次之后的 should_stop() 一律返回 True"""

    def __init__(self, stop_after=None):
        self.calls = 0
        self.stop_after = stop_after

    def should_stop(self):
        self.calls += 1
        if self.stop_after is None:
            return False
        return self.calls > self.stop_after


POINT = {"title": "1.2 总体国家安全观内涵", "id": "1001", "has_finished": False}
COURSE = {"title": "当代大学生国家安全教育", "courseId": "1", "clazzId": "2", "cpi": "3"}


def _patch_budget(budget):
    old = m.runtime_budget
    m.runtime_budget = budget
    return old


def _patch_process_job(recorder):
    old = m.process_job
    m.process_job = recorder
    return old


def _jobs_mixed():
    # 故意打乱: 页面顺序里视频、测验、文档交错, 也不能让测验跑到前面
    return [
        {"type": "video", "name": "v1.mp4"},
        {"type": "workid", "name": "本节测验"},
        {"type": "document", "name": "讲义.pdf"},
        {"type": "video", "name": "v2.mp4"},
        {"type": "workid", "name": "本节测验2"},
    ]


def test_content_before_quiz():
    """内容任务必须在测验之前, 且各自保持原有相对顺序"""
    order = []

    def recorder(chaoxing, course, job, job_info, speed):
        order.append(job["name"])
        return StudyResult.SUCCESS

    old_budget = _patch_budget(FakeBudget())
    old_job = _patch_process_job(recorder)
    try:
        result = m.process_chapter(FakeChaoxing(_jobs_mixed()), COURSE, POINT, 1.0)
    finally:
        m.runtime_budget = old_budget
        m.process_job = old_job

    expected = ["v1.mp4", "讲义.pdf", "v2.mp4", "本节测验", "本节测验2"]
    assert order == expected, f"执行顺序不对: {order} != {expected}"
    assert result == m.ChapterResult.SUCCESS, result
    print("[ok] 先学完内容, 再做本节测验")


def test_quiz_skipped_when_budget_hit():
    """内容学完之前就到时长上限, 测验不应再发起请求"""
    order = []

    def recorder(chaoxing, course, job, job_info, speed):
        order.append(job["name"])
        return StudyResult.SUCCESS

    # 开头 1 次 + 每个任务点前 1 次; stop_after=3 表示第 4 次起判定到点
    old_budget = _patch_budget(FakeBudget(stop_after=3))
    old_job = _patch_process_job(recorder)
    try:
        result = m.process_chapter(FakeChaoxing(_jobs_mixed()), COURSE, POINT, 1.0)
    finally:
        m.runtime_budget = old_budget
        m.process_job = old_job

    assert order == ["v1.mp4", "讲义.pdf"], f"到点后不应该再处理任务点: {order}"
    assert result == m.ChapterResult.SKIPPED, f"章节应标记为 SKIPPED, 实际 {result}"
    print("[ok] 到时长上限后不再做测验, 章节标记 SKIPPED")


def test_finished_chapter_short_circuits():
    """已完成章节直接返回, 不发任何任务点请求"""
    order = []

    def recorder(chaoxing, course, job, job_info, speed):
        order.append(job["name"])
        return StudyResult.SUCCESS

    old_budget = _patch_budget(FakeBudget())
    old_job = _patch_process_job(recorder)
    try:
        done_point = dict(POINT, has_finished=True)
        result = m.process_chapter(FakeChaoxing(_jobs_mixed()), COURSE, done_point, 1.0)
    finally:
        m.runtime_budget = old_budget
        m.process_job = old_job

    assert order == [], order
    assert result == m.ChapterResult.SUCCESS
    print("[ok] 已完成章节直接跳过")


if __name__ == "__main__":
    test_content_before_quiz()
    test_quiz_skipped_when_budget_hit()
    test_finished_chapter_short_circuits()
    print("\n全部通过")
