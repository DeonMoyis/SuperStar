# -*- coding: utf-8 -*-
"""任务点卡死保护 + 重播策略测试

超星有一类任务点: playTime 已经等于 duration, 但 isPassed 一直返回 false。
旧实现在这里会死循环(play_time == duration 让上报条件永远成立, 而它不可能再增长,
且旧代码跳过了 sleep, 变成无间隔刷接口)。

现在分成三种情况, 本文件逐条验证:

  1. **起点就在片尾** —— 已经全看过了, 再播一遍是重复劳动, 不重播;
  2. **从 0 播到片尾不认** —— 已经真播过一遍, 也不重播;
  3. **从中断处续播到片尾不认** —— 服务端可能认为观看不连续, 从头真播一次
     (只一次, 重播后仍不认就放弃);

另外还验证两件事:

  * 片尾不认时先按真人节奏退避重试几轮(超星落库经常滞后十几秒),
    不能 3 秒就判死;
  * 从 0 秒开始的视频, 开播前不再发 isdrag=4 的「已到片尾」探测 ——
    那等于主动告诉服务端「我把进度条拖到底了」, 会被防拖拽校验记一笔。
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
        if _isdrag == 4:
            # 冲刺/片尾重试探测: 这类测试里统一回答"没完成"
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

    播放进度是按真实时间推进的, 所以视频时长不能设太大。
    """
    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        return chaoxing.study_video(
            {"courseId": "1", "clazzId": "2"},
            {"objectid": "obj-1", "name": "卡死视频", "playTime": play_ms,
             "mid": "mid-1", "videoFaceCaptureEnc": ""},
            {},
        )
    finally:
        time.sleep = real_sleep


# ---------------------------------------------------------------- 起点即片尾
def test_start_at_end_replays_once():
    """playTime == duration 但服务端不认: 退避重试后, 仍从头真播一次

    线上的 4.2 视频就是这个形态: 第一遍已经播满, 服务端 isPassed 迟迟不置 true,
    从头真播一遍之后才认 —— 超星校验的是「连续观看」, 不是「进度数字到顶」。
    """
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    result = run_without_sleeping(chaoxing, duration * 1000)

    assert result == base.StudyResult.SKIPPED, f"重播仍失败应跳过, 实际 {result}"
    probes = [c for c in calls if c[0] == 4]
    # 第一轮: 1 次冲刺探测 + STUCK_END_RETRY 次片尾重试; 重播轮: 只有 STUCK_END_RETRY 次
    assert len(probes) == 1 + 2 * base.STUCK_END_RETRY, f"片尾探测 {len(probes)} 次, 与预期轮次不符"
    print(f"[ok] 起点即片尾: {len(probes)} 次探测, 退避重试后从头重播一次")


# ---------------------------------------------------------------- 从 0 播完不认
def test_finished_from_zero_replays_once():
    """从 0 秒真播到片尾, 服务端始终不放行: 重播一次后放弃, 不会无限重播"""
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    result = run_without_sleeping(chaoxing, 0)

    assert result == base.StudyResult.SKIPPED, f"应跳过, 实际 {result}"
    played = [c for c in calls if c[0] == 3]
    assert played, "应该有正常播放上报"
    probes = [c for c in calls if c[0] == 4]
    # 从 0 开始的视频开播前不发冲刺探测, 所以两轮各 STUCK_END_RETRY 次
    assert len(probes) == 2 * base.STUCK_END_RETRY, f"片尾探测 {len(probes)} 次, 重播次数不对"
    # 两轮播放, 每轮上报次数约 duration + STALL_LIMIT, 不该失控
    assert len(played) <= 2 * (duration + base.STALL_LIMIT + 2), f"上报次数 {len(played)} 超出预期"
    print(f"[ok] 从 0 播完不认: 重播一次后放弃, 共 {len(calls)} 次上报")


# ---------------------------------------------------------------- 续播 -> 重播一次
def test_resume_replays_exactly_once():
    """从断点续播到片尾不认: 从头真播一次(只一次), 仍不认才放弃"""
    duration = 4
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    result = run_without_sleeping(chaoxing, 2 * 1000)

    assert result == base.StudyResult.SKIPPED, f"应跳过, 实际 {result}"
    # 正常上报只在片尾才发(间隔 wait_time 30~90s), 所以用「片尾探测次数」倒推轮次:
    #   第一轮 = 1 次冲刺探测 + STUCK_END_RETRY 次片尾重试
    #   重播轮 = 0 次冲刺探测 + STUCK_END_RETRY 次片尾重试
    probes = [c for c in calls if c[0] == 4]
    assert len(probes) == 1 + 2 * base.STUCK_END_RETRY, f"重播次数不对: {len(probes)} 次探测"
    print(f"[ok] 续播不认: 从头重播一次后放弃, 共 {len(probes)} 次片尾探测 / {len(calls)} 次上报")


# ---------------------------------------------------------------- 片尾重试能救回来
def test_retry_at_end_can_rescue():
    """服务端只是落库慢: 片尾退避重试期间认了, 就应该算完成, 不该白重播"""
    duration = 3
    setup(duration)
    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.get_fid = lambda: 1024
    chaoxing.kwargs = {}
    probe_calls = {"n": 0}

    def fake_progress_log(_session, _course, _job, _job_info, _dtoken, _duration,
                          _playingTime, _type="Video", _isdrag=3, headers=None):
        if _isdrag == 4:
            probe_calls["n"] += 1
            # 第 2 次片尾探测才认(模拟服务端延迟了一轮)
            return (probe_calls["n"] >= 2, 200)
        return (False, 200)

    chaoxing.video_progress_log = fake_progress_log

    result = run_without_sleeping(chaoxing, 0)

    assert result == base.StudyResult.SUCCESS, f"片尾重试后应判定完成, 实际 {result}"
    print(f"[ok] 片尾延迟: 第 {probe_calls['n']} 次探测后认可, 未重播")


# ---------------------------------------------------------------- 开播不探测
def test_no_end_probe_when_playing_from_zero():
    """从 0 开始看的视频, 开播前不该发 isdrag=4 的「已到片尾」探测"""
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=duration)

    run_without_sleeping(chaoxing, 0)

    first = calls[0]
    assert first[0] != 4, f"首个请求不该是拖拽探测, 实际 isdrag={first[0]}"
    assert not any(isdrag == 4 and t >= duration for isdrag, t in calls[:1]), "不该一开播就报满进度"
    print("[ok] 从 0 播放: 开播前无拖拽探测, 首报就是真实播放上报")


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
                                       "mid": "mid-1", "videoFaceCaptureEnc": ""}, {})
        elapsed = time.time() - started
    finally:
        time.sleep = real_sleep

    assert result == base.StudyResult.SUCCESS, result
    assert calls[-1][1] >= duration, calls
    assert elapsed < 8, f"sleep 已被替换成空操作, 耗时应很短, 实际 {elapsed:.2f}s"
    print(f"[ok] 正常视频: 返回 SUCCESS, {len(calls)} 次上报")


if __name__ == "__main__":
    test_start_at_end_replays_once()
    test_finished_from_zero_replays_once()
    test_resume_replays_exactly_once()
    test_retry_at_end_can_rescue()
    test_no_end_probe_when_playing_from_zero()
    test_normal_video_still_succeeds()
    print("\n全部通过")
