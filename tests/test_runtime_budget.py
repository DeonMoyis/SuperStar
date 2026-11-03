# -*- coding: utf-8 -*-
"""单次运行时长限制（时间盒）测试

验证四件事:
1. 上限能正确解析, 0 / 空值 / 非法值都表示不限制
2. 到点后 should_stop() 为真, 且只提示一次
3. 排队中的视频会被播放闸门拒绝, 正在播的不受影响
4. 任务队列里的待办能被摘掉, join() 不会卡死
"""
import sys
import threading
import time
from pathlib import Path
from queue import PriorityQueue
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.runtime_budget as budget_module  # noqa: E402
from api.playback import PlaybackGate  # noqa: E402
from api.runtime_budget import RuntimeBudget, runtime_budget  # noqa: E402


# ---------------------------------------------------------------- 预算本体
def test_disabled_when_not_configured():
    budget = RuntimeBudget()
    assert budget.enabled is False
    assert budget.expired is False
    assert budget.remaining is None
    assert budget.should_stop() is False
    assert "未设置时长上限" in budget.summary()
    print("[ok] 未配置上限: 不限制, 永不 expired")


def test_invalid_values_disable_limit():
    for value in (0, "0", "", None, "abc", -5):
        budget = RuntimeBudget(value)
        assert budget.enabled is False, f"{value!r} 应视为不限制"
    print("[ok] 0 / 空 / 非法值 / 负数: 都视为不限制")


def test_expires_after_limit():
    # 0.02 分钟 = 1.2 秒
    budget = RuntimeBudget(0.02)
    assert budget.enabled is True
    assert budget.expired is False
    assert 0 < budget.remaining <= 1.3

    time.sleep(1.4)
    assert budget.expired is True
    assert budget.remaining == 0
    assert budget.should_stop() is True
    assert "已到上限" in budget.summary()
    print(f"[ok] 到点生效: 1.2 秒后 expired, summary = {budget.summary()}")


def test_reconfigure_resets_timer():
    budget = RuntimeBudget(0.01)  # 0.6 秒
    budget.configure(60)
    assert budget.expired is False
    assert budget.elapsed < 1
    assert budget.limit_seconds == 3600
    print("[ok] 重新配置会重置计时起点")


# ---------------------------------------------------------------- 与播放闸门联动
def test_gate_rejects_waiter_when_stopped():
    """一个视频在播、另一个在排队, 到点后排队那个必须立刻放弃"""
    gate = PlaybackGate()
    playing = threading.Event()
    release = threading.Event()

    def holder():
        with gate.hold("正在播的视频") as acquired:
            assert acquired is True
            playing.set()
            release.wait(8)

    player = threading.Thread(target=holder, daemon=True)
    player.start()
    assert playing.wait(3), "占用播放位的线程没起来"

    result = {}

    def waiter():
        with gate.hold("排队中的视频", should_stop=lambda: True) as acquired:
            result["acquired"] = acquired

    started = time.time()
    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    thread.join(5)
    waited = time.time() - started

    release.set()
    player.join(3)

    assert result.get("acquired") is False, "到点后不该再拿到播放位"
    assert waited < 3, f"应在 1~2 秒内放弃, 实际等了 {waited:.2f}s"
    print(f"[ok] 排队中被拒绝: {waited:.2f}s 内放弃, 正在播的视频不受影响")


def test_gate_acquires_when_not_stopped():
    gate = PlaybackGate()
    with gate.hold("正常视频", should_stop=lambda: False) as acquired:
        assert acquired is True
    print("[ok] 未到点时正常拿到播放位")


# ---------------------------------------------------------------- 任务队列
def test_abort_pending_drains_queue():
    """队列里的待办全部标记跳过, unfinished_tasks 归零(否则 join 会卡死)"""
    from main import ChapterTask, ChapterResult, JobProcessor

    processor = object.__new__(JobProcessor)
    processor.task_queue = PriorityQueue()
    processor.skipped_tasks = []
    processor.STOP_POINT = JobProcessor.STOP_POINT

    tasks = [ChapterTask(index=i, point={"title": f"章节{i}"}) for i in range(4)]
    for task in tasks:
        processor.task_queue.put(task)

    processor._abort_pending()

    assert len(processor.skipped_tasks) == 4, processor.skipped_tasks
    assert all(t.result == ChapterResult.SKIPPED for t in tasks)
    assert processor.task_queue.unfinished_tasks == 0, "队列必须清空, 否则 join() 会一直阻塞"
    print("[ok] 到点清空队列: 4 个待办标记 SKIPPED, unfinished_tasks 归零")


def test_processor_stops_without_hanging():
    """端到端: 到点后不再开新章节, run() 必须能正常返回而不是卡死"""
    import main as main_mod

    runtime_budget.configure(0.02)  # 1.2 秒预算

    executed = []

    def fake_process_chapter(chaoxing, course, point, speed):
        executed.append(point["title"])
        time.sleep(0.8)   # 模拟一个任务正在执行
        return main_mod.ChapterResult.SUCCESS

    original = main_mod.process_chapter
    main_mod.process_chapter = fake_process_chapter
    try:
        tasks = [
            main_mod.ChapterTask(index=i, point={"title": f"章节{i}"})
            for i in range(8)
        ]
        processor = main_mod.JobProcessor(
            None, {"title": "测试课程"}, tasks, {"jobs": 1, "speed": 1.0}
        )
        started = time.time()
        processor.run()
        elapsed = time.time() - started
    finally:
        main_mod.process_chapter = original
        runtime_budget.configure(0)

    assert len(executed) < 8, f"应该提前停, 实际执行了 {len(executed)} 个"
    assert len(processor.skipped_tasks) == 8 - len(executed), (
        f"没执行的应记为跳过, executed={len(executed)} skipped={len(processor.skipped_tasks)}"
    )
    assert elapsed < 10, f"run() 应在到点后几秒内返回, 实际 {elapsed:.2f}s"
    print(
        f"[ok] 调度器优雅停止: 执行 {len(executed)} 个 / 跳过 {len(processor.skipped_tasks)} 个, "
        f"run() 在 {elapsed:.2f}s 内正常返回"
    )


def test_skipped_is_not_failure():
    """SKIPPED 不能算失败, 否则会触发无意义的重试"""
    from api.base import StudyResult

    assert StudyResult.SKIPPED.is_failure() is False
    assert StudyResult.SKIPPED.is_success() is False
    assert StudyResult.SKIPPED.is_skipped() is True
    assert StudyResult.ERROR.is_failure() is True
    assert StudyResult.SUCCESS.is_success() is True
    print("[ok] SKIPPED 既不算成功也不算失败, 不会触发重试")


# ---------------------------------------------------------------- 配置注入
def test_cli_and_config_plumbing():
    from main import build_config_from_args

    base_args = dict(
        use_cookies=False, username="u", password="p", list=None,
        speed=1.0, jobs=1, notopen_action="retry", verbose=False,
        auto_answer=False, answer_only=False, answer_submit=None, answer_debug=False,
        deepseek_key=None, ai_model=None, ai_mode=None,
    )

    common, _, _ = build_config_from_args(SimpleNamespace(max_runtime=90, **base_args))
    assert common["max_runtime_minutes"] == 90

    common, _, _ = build_config_from_args(SimpleNamespace(max_runtime=None, **base_args))
    assert common["max_runtime_minutes"] is None, "不传就是不限制"

    # 全局单例能被配置驱动
    runtime_budget.configure(90)
    assert runtime_budget.enabled is True
    assert runtime_budget.limit_seconds == 5400
    runtime_budget.configure(0)
    assert runtime_budget.enabled is False
    print("[ok] 配置传递: --max-runtime 90 -> 5400 秒, 0 -> 不限制")


def main():
    test_disabled_when_not_configured()
    test_invalid_values_disable_limit()
    test_expires_after_limit()
    test_reconfigure_resets_timer()
    test_gate_rejects_waiter_when_stopped()
    test_gate_acquires_when_not_stopped()
    test_abort_pending_drains_queue()
    test_processor_stops_without_hanging()
    test_skipped_is_not_failure()
    test_cli_and_config_plumbing()
    print("\n全部运行时长限制测试通过")


if __name__ == "__main__":
    main()
