# -*- coding: utf-8 -*-
"""任务点卡死保护 + 「未完成就从头重播」测试

超星有一类任务点: playTime 已经等于 duration, 但 isPassed 一直返回 false
(典型原因是还有弹窗题/章节测验没做)。旧实现在这里会死循环:

  - play_time == duration 让上报条件永远成立
  - play_time 不可能再增长, 循环永远退不出去
  - 而且 play_time == duration 时旧代码跳过了 sleep, 变成无间隔刷接口

结果就是整个 job 挂到 Actions 超时, 同时把接口刷到风控。
现在的处理是两级:

  1. 限次重试后仍不认账 -> 判定「卡住」;
  2. 卡住就忽略已记录进度, 从 0 秒重新播一遍(本文件重点验证这条);
     重播还不行才真正放弃, 保证不会无限重播。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.base as base  # noqa: E402


class NullBar:
    """替代 tqdm, 避免测试输出被进度条刷屏"""

    def __init__(self, **kwargs):
        self.n = kwargs.get("initial", 0)

    def refresh(self):
        pass

    def close(self):
        pass


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200
        self.text = "{}"

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, duration):
        self.duration = duration

    def get(self, url, **kwargs):
        return FakeResponse({
            "status": "success",
            "duration": self.duration,
            "dtoken": "fake-dtoken",
            "crc": "fake-crc",
            "key": "fake-key",
        })


def make_chaoxing(duration, pass_at):
    """伪造 Chaoxing 实例

    pass_at: 进度达到多少秒时服务端才认通过; None 表示永远不认通过
    """
    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.get_fid = lambda: 1024
    chaoxing.kwargs = {}
    calls = []

    def fake_progress_log(_session, _course, _job, _job_info, _dtoken, _duration,
                          _playingTime, _type="Video", _isdrag=3, headers=None):
        calls.append((_isdrag, int(_playingTime)))
        # _isdrag=4 是开播前的"是否已完成"探测, 这里统一回答"没完成",
        # 这样才能真正走进播放循环(否则会被"瞬间完成"提前放行)
        if _isdrag == 4:
            return (False, 200)
        if pass_at is None:
            return (False, 200)
        return (_playingTime >= pass_at, 200)

    chaoxing.video_progress_log = fake_progress_log
    return chaoxing, calls


def setup(duration):
    base.SessionManager.get_session = staticmethod(lambda: FakeSession(duration))
    base.tqdm = NullBar


def run_without_sleeping(chaoxing, play_ms):
    """把 sleep 换成空操作, 否则退避会让测试跑上十几秒

    注意: 播放进度是按真实时间推进的, 所以视频时长不能设太大,
    否则测试本身就要等那么久。
    """
    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        return chaoxing.study_video(
            {"courseId": "1", "clazzId": "2"},
            {"objectid": "obj-1", "name": "卡死视频", "playTime": play_ms, "mid": "mid-1"},
            {},
        )
    finally:
        time.sleep = real_sleep


# ---------------------------------------------------------------- 起点即片尾
def test_play_time_at_end_replays_from_zero():
    """playTime == duration 但服务端不认通过: 限次重试后判定卡住, 并从头重播"""
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    started = time.time()
    result = run_without_sleeping(chaoxing, duration * 1000)
    elapsed = time.time() - started

    assert result == base.StudyResult.SKIPPED, f"重播仍失败应跳过, 实际 {result}"
    probes = [c for c in calls if c[0] == 4]
    # 冲刺探测 + 限次重试, 不该出现几十上百次
    assert len(probes) <= 1 + base.STUCK_END_RETRY, f"片尾探测 {len(probes)} 次, 超出预期"
    # 关键: 卡住之后确实从 0 开始重播了(旧实现这里直接放弃, 不会有 isdrag=3 的上报)
    assert any(isdrag == 3 for isdrag, _ in calls), "没有从头重播, 仍是旧行为"
    assert elapsed < 20, f"耗时 {elapsed:.2f}s, 可能重播了不止一次"
    print(f"[ok] 起点即片尾: {len(probes)} 次探测后从头重播, 最终 {result.name}, 耗时 {elapsed:.2f}s")


# ---------------------------------------------------------------- 播完不放行
def test_finished_but_not_passed_gives_up():
    """从头播到片尾, 服务端始终不放行: 连续无进展后判定卡死, 重播一次后放弃"""
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    started = time.time()
    result = run_without_sleeping(chaoxing, 0)
    elapsed = time.time() - started

    assert result == base.StudyResult.SKIPPED, f"应判定卡死并跳过, 实际 {result}"
    assert elapsed < 20, f"耗时 {elapsed:.2f}s, 卡死检测或重播次数失控"
    assert len(calls) <= 2 * (duration + base.STALL_LIMIT + 3), f"上报次数 {len(calls)} 超出预期"
    print(f"[ok] 播完不放行: {len(calls)} 次上报, {elapsed:.2f}s 后放弃")


# ---------------------------------------------------------------- 只重播一次
def test_replays_only_once():
    """重播必须只做一次, 否则就是换了个形式的死循环"""
    duration = 2
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    run_without_sleeping(chaoxing, 0)

    # 每次尝试最多 1 次冲刺探测; 重播模式不再探测, 所以探测次数直接暴露尝试次数
    probes = [c for c in calls if c[0] == 4]
    assert len(probes) <= 1 + base.STUCK_END_RETRY, probes
    print(f"[ok] 只重播一次: 全程 {len(probes)} 次冲刺探测, 共 {len(calls)} 次上报")


# ---------------------------------------------------------------- 正常放行
def test_normal_video_still_succeeds():
    """回归: 正常视频不受卡死保护影响, 仍然按真实时长播完后返回 SUCCESS"""
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=duration)

    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        started = time.time()
        result = chaoxing.study_video({"courseId": "1", "clazzId": "2"},
                                      {"objectid": "obj-1", "name": "正常视频", "playTime": 0,
                                       "mid": "mid-1"}, {})
        elapsed = time.time() - started
    finally:
        time.sleep = real_sleep

    assert result == base.StudyResult.SUCCESS, result
    assert calls[0][0] == 4, "首个请求应为冲刺检测(_isdrag=4)"
    assert calls[-1][1] >= duration, calls
    assert elapsed < 8, f"sleep 已被替换成空操作, 耗时应很短, 实际 {elapsed:.2f}s"
    print(f"[ok] 正常视频: 返回 SUCCESS, {len(calls)} 次上报")


if __name__ == "__main__":
    test_play_time_at_end_replays_from_zero()
    test_finished_but_not_passed_gives_up()
    test_replays_only_once()
    test_normal_video_still_succeeds()
    print("\n全部通过")
