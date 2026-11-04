# -*- coding: utf-8 -*-
"""任务点卡死保护 + 重播策略测试

超星有一类任务点: playTime 已经等于 duration, 但 isPassed 一直返回 false。

现在的策略是「一直真播到通过为止, 但有轮数上限」:

  1. **片尾不认先按真人节奏退避几轮**(STUCK_END_RETRY 次, 每轮等 12~25s)。
     关键: 全程用 isdrag=3(正常播放) + 进度 duration-1, **不用 isdrag=4** ——
     isdrag 是超星的播放行为标记, 4 代表拖动进度条, 用它报片尾等于反复说
     「我又拖到底了」, 这类上报不会被计入完成观看。线上 5.1 那个 9:48 的视频
     就是这么连续 5 轮(88 秒)一次都没认, 而重播后用 3 报一次就过了;
  2. 退避仍不认 -> **忽略已记录进度, 从 0 秒真播**, 直到通过或播满
     MAX_FULL_RESTART 轮(否则一个永远过不去的任务点会吃光整次运行时长);
  3. 中途卡死(进度推不动)同样从头重播;
  4. 人脸采集 / 已被判定永久失败的任务点, 播放前直接跳过, 一次都不重播。

不走「退回最后 30 秒续播」的取巧路线: 进度倒退本身就是拖拽特征, 反而更容易被抓。
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


def make_chaoxing(duration, pass_at=None, rescue_at=None):
    """伪造 Chaoxing 实例

    pass_at:   正常播放的进度达到多少秒时服务端认通过; None = 永远不认
    rescue_at: 第几次「片尾退避重试」时认通过; None = 退避也不认

    片尾退避的识别特征: 进度是 duration-1(正常播放只在 play_time==duration 时上报,
    而测试里 wait_time 是 30~90 秒, 2~4 秒的视频根本走不到)。
    """
    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.get_fid = lambda: 1024
    chaoxing.kwargs = {}
    calls = []
    retry_state = {"n": 0}

    def fake_progress_log(_session, _course, _job, _job_info, _dtoken, _duration,
                          _playingTime, _type="Video", _isdrag=3, headers=None):
        playing = int(_playingTime)
        calls.append((_isdrag, playing))
        if playing == duration - 1:
            retry_state["n"] += 1
            if rescue_at is not None and retry_state["n"] >= rescue_at:
                return (True, 200)
            return (False, 200)
        if pass_at is None:
            return (False, 200)
        return (playing >= pass_at, 200)

    chaoxing.video_progress_log = fake_progress_log
    return chaoxing, calls


def setup(duration):
    base.SessionManager.get_session = staticmethod(lambda: FakeSession(duration))
    base.tqdm = NullBar


def run_study(chaoxing, play_ms):
    """跑一遍 study_video, 同时记录「真正播了几轮」

    把 sleep 换成空操作, 否则退避(12~25s × 3)会让测试跑上一分多钟。
    播放进度按真实时间推进, 所以视频时长要设小, 否则每轮就是实打实地等那么久。
    """
    rounds = []
    real_once = base.Chaoxing._study_video_once

    def counting_once(self, *args, **kwargs):
        rounds.append(bool(kwargs.get("restart_from_zero")))
        return real_once(self, *args, **kwargs)

    chaoxing._study_video_once = counting_once.__get__(chaoxing, base.Chaoxing)

    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        result = chaoxing.study_video(
            {"courseId": "1", "clazzId": "2"},
            {"objectid": "obj-1", "name": "卡死视频", "playTime": play_ms,
             "mid": "mid-1", "videoFaceCaptureEnc": ""},
            {},
        )
    finally:
        time.sleep = real_sleep
    return result, rounds


# ---------------------------------------------------------------- 退避语义
def test_retry_at_end_uses_normal_playback_semantics():
    """片尾退避必须用 isdrag=3 + 进度 duration-1, 全程不能出现 isdrag=4

    这是线上 5.1 那个视频暴露的问题: 用 4(拖动进度条)去报片尾, 服务端一次都不认。
    """
    duration = 2
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    run_study(chaoxing, duration * 1000)

    probes = [c for c in calls if c[0] == 4]
    assert not probes, f"不该再发 isdrag=4(拖动语义)的上报, 实际 {probes}"
    at_end_retries = [c for c in calls if c[1] == duration - 1]
    assert at_end_retries, "应该有片尾退避重试"
    assert all(c[0] == 3 for c in at_end_retries), at_end_retries
    print(f"[ok] 片尾退避: {len(at_end_retries)} 次上报全部 isdrag=3 / 进度 duration-1")


# ---------------------------------------------------------------- 退避能救回来
def test_retry_at_end_can_rescue():
    """服务端只是落库慢: 退避期间认了就算完成, 不该白重播"""
    duration = 2
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None, rescue_at=2)

    result, rounds = run_study(chaoxing, 0)

    assert result == base.StudyResult.SUCCESS, f"退避后应判定完成, 实际 {result}"
    assert len(rounds) == 1, f"退避就救回来了, 不该重播, 实际播了 {len(rounds)} 轮"
    print(f"[ok] 片尾延迟: 第 2 次退避后认可, 未重播")


# ---------------------------------------------------------------- 一直播到上限
def test_finished_from_zero_replays_until_limit():
    """从 0 播完仍不认: 一直从 0 重播, 播满 MAX_FULL_RESTART 轮才放弃"""
    duration = 2
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    result, rounds = run_study(chaoxing, 0)

    assert result == base.StudyResult.SKIPPED, f"始终不认应跳过, 实际 {result}"
    assert rounds[0] is False, "第一轮应是「按已记录进度播」"
    assert all(r is True for r in rounds[1:]), f"后续每轮都必须从 0 秒重播, 实际 {rounds}"
    assert len(rounds) == 1 + base.MAX_FULL_RESTART, (
        f"轮数应为 1 + MAX_FULL_RESTART({base.MAX_FULL_RESTART}), 实际 {len(rounds)}"
    )
    played = [c for c in calls if c[0] == 3 and c[1] >= duration]
    assert played, "每轮都应该真播到片尾"
    print(f"[ok] 始终不认: 从 0 重播 {base.MAX_FULL_RESTART} 轮后放弃, 共 {len(calls)} 次上报")


# ---------------------------------------------------------------- 续播 -> 从 0 重播
def test_resume_replays_from_zero():
    """从断点续播到片尾不认: 忽略已记录进度, 从 0 秒真播"""
    duration = 4
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    result, rounds = run_study(chaoxing, 2 * 1000)

    assert result == base.StudyResult.SKIPPED, f"应跳过, 实际 {result}"
    assert rounds[0] is False and rounds[1] is True, rounds
    print(f"[ok] 续播不认: 改为从 0 秒真播, 共 {len(rounds)} 轮 / {len(calls)} 次上报")


# ---------------------------------------------------------------- 起点即片尾
def test_start_at_end_replays():
    """playTime 已经等于 duration 但服务端不认: 退避后仍从头真播"""
    duration = 2
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=None)

    result, rounds = run_study(chaoxing, duration * 1000)

    assert result == base.StudyResult.SKIPPED, f"重播仍失败应跳过, 实际 {result}"
    assert rounds == [False] + [True] * base.MAX_FULL_RESTART, f"轮次不对: {rounds}"
    print(f"[ok] 起点即片尾: 退避后从头重播 {base.MAX_FULL_RESTART} 轮")


# ---------------------------------------------------------------- 开播不探测
def test_no_end_probe_when_playing_from_zero():
    """从 0 开始看的视频, 开播前不该先报一个「已到片尾」的进度"""
    duration = 3
    setup(duration)
    chaoxing, calls = make_chaoxing(duration, pass_at=duration)

    run_study(chaoxing, 0)

    first_isdrag, first_time = calls[0]
    assert first_isdrag != 4, f"首个请求不该是拖拽上报, 实际 isdrag={first_isdrag}"
    assert first_time >= 1, f"首个上报不该是紧贴开播的满进度, 实际 {first_time}s"
    print("[ok] 从 0 播放: 开播前无片尾探测, 首报就是真实播放上报")


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
    test_retry_at_end_uses_normal_playback_semantics()
    test_retry_at_end_can_rescue()
    test_finished_from_zero_replays_until_limit()
    test_resume_replays_from_zero()
    test_start_at_end_replays()
    test_no_end_probe_when_playing_from_zero()
    test_normal_video_still_succeeds()
    print("\n全部通过")
