# -*- coding: utf-8 -*-
"""播放 / 作答节奏测试: 1 倍速固定 + 串行闸门

不需要登录, 也不需要真实网络。核心验证三件事:
1. 任何倍速配置都会被压到 1.0
2. 两个视频同时处理时, 真实耗时是相加而不是重叠(否则等于变相倍速)
3. 章节测验共用同一个作答位, 同一时刻只有一份在作答/提交
"""
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.base as base  # noqa: E402
from api.playback import AnswerGate, FORCED_SPEED, PlaybackGate, normalize_speed  # noqa: E402


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


def make_chaoxing(duration):
    """伪造一个 Chaoxing 实例, 只实现 study_video 需要的部分"""
    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.get_fid = lambda: 1024
    chaoxing.kwargs = {}
    log_calls = []

    def fake_progress_log(_session, _course, _job, _job_info, _dtoken, _duration,
                          _playingTime, _type="Video", _isdrag=3, headers=None):
        log_calls.append((_isdrag, int(_playingTime)))
        # _isdrag=4 是开播前的"是否已完成"探测, 这里模拟服务端回答"没完成"
        if _isdrag == 4:
            return (False, 200)
        # 只有进度播完服务端才放行
        return (_playingTime >= duration, 200)

    chaoxing.video_progress_log = fake_progress_log
    return chaoxing, log_calls


def make_job(name="测试视频", play_ms=0):
    return {"objectid": "obj-1", "name": name, "playTime": play_ms}


def patch_session(duration):
    base.SessionManager.get_session = staticmethod(lambda: FakeSession(duration))


def patch_tqdm():
    base.tqdm = NullBar


# ---------------------------------------------------------------- 倍速归一化
def test_speed_is_forced_to_one():
    assert normalize_speed(2.0) == FORCED_SPEED
    assert normalize_speed(1.5) == FORCED_SPEED
    assert normalize_speed(1.0) == FORCED_SPEED
    assert normalize_speed(0.5) == FORCED_SPEED, "低于 1 倍速也统一拉回 1"
    assert normalize_speed("2") == FORCED_SPEED
    assert normalize_speed(None) == FORCED_SPEED
    assert normalize_speed("abc") == FORCED_SPEED
    print("[ok] 倍速归一化: 2x / 1.5x / 0.5x / 字符串 / 空值 全部压到 1.0")


def test_command_line_speed_ignored():
    from main import build_config_from_args

    args = SimpleNamespace(
        use_cookies=False, username="u", password="p", list=None,
        speed=2.0, jobs=4, notopen_action="retry", verbose=False,
        auto_answer=False, answer_only=False, answer_submit=None, answer_debug=False,
        deepseek_key=None, ai_model=None, ai_mode=None,
    )
    common_config, _, _ = build_config_from_args(args)
    assert common_config["speed"] == FORCED_SPEED, common_config["speed"]
    print("[ok] 命令行 --speed 2.0 被忽略, 仍为 1.0")


# ---------------------------------------------------------------- 播放闸门
def test_gate_is_exclusive():
    gate = PlaybackGate()
    overlap = []
    active = {"count": 0}

    def worker(name):
        with gate.hold(name):
            active["count"] += 1
            overlap.append(active["count"])
            time.sleep(0.25)
            active["count"] -= 1

    threads = [threading.Thread(target=worker, args=(f"v{i}",)) for i in range(4)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    assert max(overlap) == 1, f"同一时刻只允许 1 个播放入口, 实际 {max(overlap)}"
    assert elapsed >= 0.9, f"4 个 0.25s 的任务串行应 >= 1s, 实际 {elapsed:.2f}s"
    print(f"[ok] 播放闸门互斥: 4 个并发任务最大同时占用 {max(overlap)}, 总耗时 {elapsed:.2f}s")


# ---------------------------------------------------------------- 真实推进速率
def test_single_video_runs_at_real_time():
    """4 秒的视频, 1 倍速下实际耗时应接近 4 秒"""
    duration = 4
    patch_session(duration)
    patch_tqdm()
    chaoxing, log_calls = make_chaoxing(duration)

    started = time.time()
    result = chaoxing.study_video({"courseId": "1", "clazzId": "2"}, make_job(), {})
    elapsed = time.time() - started

    assert result == base.StudyResult.SUCCESS, result
    # 循环以 1 秒为粒度推进, 播完还要等下一轮才上报, 所以会多 1~2 秒
    assert duration - 0.5 <= elapsed <= duration + 3.5, (
        f"{duration}s 视频用 1 倍速应约 {duration}s, 实际 {elapsed:.2f}s"
    )
    assert log_calls[0][0] == 4, "首个请求应为冲刺检测(_isdrag=4)"
    print(f"[ok] 单视频 1 倍速: 视频 {duration}s, 实际耗时 {elapsed:.2f}s")


def test_two_videos_do_not_overlap():
    """两个 3 秒视频并发处理, 总耗时必须是 6 秒而不是 3 秒"""
    duration = 3
    patch_session(duration)
    patch_tqdm()

    chaoxing_a, _ = make_chaoxing(duration)
    chaoxing_b, _ = make_chaoxing(duration)

    results = {}

    def run(tag, chaoxing):
        results[tag] = chaoxing.study_video({"courseId": "1", "clazzId": "2"}, make_job(tag), {})

    started = time.time()
    threads = [
        threading.Thread(target=run, args=("A", chaoxing_a)),
        threading.Thread(target=run, args=("B", chaoxing_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    assert results["A"] == base.StudyResult.SUCCESS
    assert results["B"] == base.StudyResult.SUCCESS
    assert elapsed >= duration * 2 - 0.5, (
        f"两个 {duration}s 视频串行播放应约 {duration * 2}s, 实际 {elapsed:.2f}s "
        f"(并发播放会让耗时接近 {duration}s, 等价于倍速)"
    )
    print(f"[ok] 双视频串行: 2 x {duration}s, 实际总耗时 {elapsed:.2f}s")


def test_start_position_skips_watched_part():
    """已有进度时应只补剩余部分, 这是正常现象而不是倍速"""
    duration = 6
    patch_session(duration)
    patch_tqdm()
    chaoxing, _ = make_chaoxing(duration)

    started = time.time()
    result = chaoxing.study_video(
        {"courseId": "1", "clazzId": "2"}, make_job(play_ms=4000), {}
    )
    elapsed = time.time() - started

    assert result == base.StudyResult.SUCCESS
    assert elapsed <= 4.5, f"已有 4s 进度只该补 2s, 实际 {elapsed:.2f}s"
    print(f"[ok] 断点续播: 视频 {duration}s 已看 4s, 本次耗时 {elapsed:.2f}s")


def test_answer_gate_is_exclusive():
    """测验也要排队: 同一时刻只能有一份在作答/提交"""
    gate = AnswerGate()
    overlap = []
    active = {"count": 0}

    def worker(name):
        with gate.hold(name) as acquired:
            assert acquired is True
            active["count"] += 1
            overlap.append(active["count"])
            time.sleep(0.2)
            active["count"] -= 1

    threads = [threading.Thread(target=worker, args=(f"测验{i}",)) for i in range(3)]
    started = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.time() - started

    assert max(overlap) == 1, f"同一时刻只允许 1 份测验在作答, 实际 {max(overlap)}"
    assert elapsed >= 0.55, f"3 个 0.2s 的测验串行应 >= 0.6s, 实际 {elapsed:.2f}s"
    print(f"[ok] 作答闸门互斥: 3 份测验串行, 总耗时 {elapsed:.2f}s")


def test_play_time_unit_is_normalized():
    """403 恢复时 playTime 可能是毫秒, 不能当成秒直接判定播完"""
    from api.base import normalize_play_time

    # 秒: 原样返回
    assert normalize_play_time(120, 600) == 120
    # 毫秒: 超过总时长, 自动除 1000
    assert normalize_play_time(120000, 600) == 120
    # 空值 / 非法值
    assert normalize_play_time(None, 600) == 0
    assert normalize_play_time("abc", 600) == 0
    print("[ok] 进度单位归一: 120s / 120000ms 都得到 120 秒, 空值返回 0")


def main():
    test_speed_is_forced_to_one()
    test_command_line_speed_ignored()
    test_gate_is_exclusive()
    test_answer_gate_is_exclusive()
    test_play_time_unit_is_normalized()
    test_single_video_runs_at_real_time()
    test_two_videos_do_not_overlap()
    test_start_position_skips_watched_part()
    print("\n全部播放节奏测试通过")


if __name__ == "__main__":
    main()
