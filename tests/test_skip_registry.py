# -*- coding: utf-8 -*-
"""跳过记录簿 + 「人脸采集视频直接跳过」测试

背景(真实踩到的):
  2.2 那个视频开了人脸采集, 进度推到片尾服务端照样 isPassed=false, 还回 403。
  旧流程是「卡住 -> 按策略从 0 秒重播」, 于是一个 15 分钟的视频白播两遍,
  Actions 时长被吃光, 而且下次运行对这件事一无所知, 又从头再来一遍。

现在的规则:
  1. 播放之前就能认出人脸采集 -> 直接跳过, 一次上报都不发;
     **但判定口径很严**: 只认明确的开关字段和像真密文的凭据, 字段非空不算
     (旧口径把「视频中间插了一道题」的课程误判成人脸, 整节跳过);
  2. 卡片/接口里带弹窗题的视频**不按人脸跳过** —— 卡住的真实原因是题没答;
  3. 跳过原因写进记录簿(skip_registry.json), permanent 条目下次运行直接跳;
     旧版(v1)的人脸结论会在加载时丢弃, 那些视频重新走一遍判定;
  4. 403/风控这类「下次可能还能成」的原因只记录, 不阻止下次重试。
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import api.base as base  # noqa: E402
from api.skip_registry import (  # noqa: E402
    REASON_FACE_CAPTURE,
    REASON_FORBIDDEN,
    SkipRegistry,
    configure_default_registry,
    get_default_registry,
)


class NullBar:
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
    def get(self, url, **kwargs):
        return FakeResponse({
            "status": "success", "duration": 3, "dtoken": "d",
            "crc": "c", "key": "k",
        })


def make_chaoxing():
    """伪造 Chaoxing 实例, video_progress_log 只记账不真发请求"""
    chaoxing = object.__new__(base.Chaoxing)
    chaoxing.get_fid = lambda: 1024
    chaoxing.kwargs = {}
    chaoxing.skip_face_capture = True
    chaoxing.skipped_points = []
    chaoxing.stuck_points = []
    calls = []

    def fake_progress_log(*args, **kwargs):
        # 播到片尾就放行, 免得测试真等视频时长 + 多轮重播
        calls.append(kwargs)
        playing = args[6] if len(args) > 6 else kwargs.get("_playingTime", 0)
        return (int(playing) >= 3, 200)

    chaoxing.video_progress_log = fake_progress_log
    return chaoxing, calls


def setup_base():
    base.SessionManager.get_session = staticmethod(lambda: FakeSession())
    base.tqdm = NullBar


# ------------------------------------------------------------ 记录簿本身
def test_registry_persist_and_reload():
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    reg = SkipRegistry(path)
    job = {"jobid": "1001", "objectid": "obj-1", "name": "2.2政治安全的主要内容.mp4"}
    reg.record("video", job, REASON_FACE_CAPTURE, course="形势与政策")

    assert os.path.exists(path), "记录应落盘"
    again = SkipRegistry(path)
    entry = again.get("video", job)
    assert entry and entry["reason_code"] == REASON_FACE_CAPTURE, entry
    assert entry["permanent"] is True, "人脸采集必须标成永久, 否则下次还会重播"
    assert entry["course"] == "形势与政策"

    # 再记一次只累加次数, 不会产生两条
    again.record("video", job, REASON_FACE_CAPTURE)
    assert again.stats()["total"] == 1, again.stats()
    assert again.get("video", job)["attempts"] == 2
    print("[ok] 记录簿: 落盘/重载/同题去重, 人脸采集标记为永久")


def test_retryable_reason_not_permanent():
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    reg = SkipRegistry(path)
    job = {"jobid": "2002", "name": "被风控的视频.mp4"}
    reg.record("video", job, REASON_FORBIDDEN)
    assert reg.get("video", job)["permanent"] is False, "403 不是死局, 下次要重试"
    assert reg.should_skip("video", job) is None, "可重试的原因不该阻止下次运行"
    print("[ok] 403/风控只记录不阻止重试")


def test_summary_lines_readable():
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    reg = SkipRegistry(path)
    reg.record("video", {"jobid": "9", "name": "人脸视频.mp4"}, REASON_FACE_CAPTURE)
    lines = reg.summary_lines()
    assert len(lines) == 1 and "人脸视频.mp4" in lines[0], lines
    assert "不再重试" in lines[0], lines
    print("[ok] 汇总文案: " + lines[0])


# ------------------------------------------------------------ 播放前就跳过
def test_face_capture_video_skipped_without_any_request():
    """人脸采集的视频: 一次上报都不该发, 更不该重播"""
    setup_base()
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    configure_default_registry(path)
    chaoxing, calls = make_chaoxing()

    job = {
        "jobid": "1664437997416549",
        "objectid": "2670a79b",
        "name": "2.2政治安全的主要内容.mp4",
        "playTime": 900000,
        "videoFaceCaptureEnc": "701f717922e30129e914bcec53d18d3f",
    }
    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        started = real_sleep(0)  # 占位, 保证下面 time.time 基准正常
        t0 = time.time()
        result = chaoxing.study_video({"courseId": "1", "title": "形势与政策"}, job, {})
        elapsed = time.time() - t0
    finally:
        time.sleep = real_sleep

    assert result == base.StudyResult.SKIPPED, result
    assert not calls, f"人脸视频不该发任何进度上报, 实际发了 {len(calls)} 次"
    assert elapsed < 1.5, f"应立刻返回, 实际 {elapsed:.2f}s"

    # 记录: 本次汇总 + 落盘
    assert chaoxing.skipped_points, "应记入本次跳过清单"
    entry = get_default_registry().get("video", job)
    assert entry and entry["reason_code"] == REASON_FACE_CAPTURE, entry
    assert entry["permanent"] is True
    print(f"[ok] 人脸采集视频: 0 次上报, {elapsed:.2f}s 直接跳过, 原因已落盘")


def test_quiz_video_is_not_skipped_as_face_capture():
    """带弹窗题的视频即使有人脸标记也要照常播放

    这就是用户核对出来的那种情况: 卡片里其实只是一道视频中的题目,
    被旧口径误判成「人脸采集」整节跳过, 任务点一直空着。
    """
    setup_base()
    configure_default_registry(os.path.join(tempfile.mkdtemp(), "skip_registry.json"))
    chaoxing, calls = make_chaoxing()

    job = {
        "jobid": "555", "objectid": "obj-555", "name": "带弹窗题的视频.mp4",
        "playTime": 0,
        # 明确开关为 1(即使真的开了人脸), 但卡片里有题 -> 应该照常播
        "videoFaceCapture": 1,
        "raw_card": {
            "jobid": "555", "type": "video", "videoFaceCapture": 1,
            "property": {
                "name": "带弹窗题的视频.mp4",
                "questions": [{
                    "id": "q1", "title": "经济安全的基础是什么?",
                    "options": "A.军事力量\nB.经济实力", "type": "single", "time": 1,
                }],
            },
        },
    }
    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        result = chaoxing.study_video({"courseId": "1", "title": "课"}, job, {})
    finally:
        time.sleep = real_sleep

    assert result == base.StudyResult.SUCCESS, f"有题就该照常播完, 实际 {result}"
    assert calls, "应该真的发进度上报, 而不是被跳过"
    assert not chaoxing.skipped_points, "不该记成跳过"
    print("[ok] 带弹窗题的视频: 即使有脸标记也照常播放, 不会被误跳")


def test_legacy_face_record_is_dropped():
    """旧版(v1)的「人脸采集」结论不可信: 加载时丢弃, 视频重新走一遍判定"""
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    payload = {
        "version": 1,
        "entries": {
            "video:old-1": {
                "key": "video:old-1", "kind": "video", "name": "被误判的视频.mp4",
                "reason_code": REASON_FACE_CAPTURE, "reason": "该视频开启人脸采集",
                "permanent": True, "attempts": 1,
            }
        },
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    reg = SkipRegistry(path)
    assert reg.stats()["total"] == 0, f"v1 的人脸记录应被丢弃, 实际 {reg.stats()}"
    print("[ok] 旧版人脸记录: 加载时丢弃, 视频会被重新检查")


def test_stale_face_record_revoked_on_recheck():
    """记录簿里还留着旧的人脸记录, 但按新口径复核不成立 -> 撤销并照常播放"""
    setup_base()
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    job = {"jobid": "888", "objectid": "obj-888", "name": "被误判的视频.mp4",
           "playTime": 0, "videoFaceCaptureEnc": "0"}

    old = SkipRegistry(path)
    old.record("video", job, REASON_FACE_CAPTURE, course="某课")
    old.save(force=True)

    configure_default_registry(path)
    chaoxing, calls = make_chaoxing()
    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        result = chaoxing.study_video({"courseId": "1", "title": "某课"}, job, {})
    finally:
        time.sleep = real_sleep

    assert result == base.StudyResult.SUCCESS, f"复核不成立应照常播, 实际 {result}"
    assert calls, "应该真的发进度上报"
    assert get_default_registry().get("video", job) is None, "过期记录应被撤销"
    print("[ok] 旧记录复核: 不成立时撤销, 视频照常播放")


def test_recorded_permanent_skips_next_run():
    """跨运行: 上次记为永久跳过, 这次开播前就不试了"""
    setup_base()
    path = os.path.join(tempfile.mkdtemp(), "skip_registry.json")
    # 带上真密文, 这样「复核」仍然成立, 才会真的跳过
    job = {"jobid": "777", "name": "上次已放弃的视频.mp4",
           "videoFaceCaptureEnc": "701f717922e30129e914bcec53d18d3f"}

    old = SkipRegistry(path)
    old.record("video", job, REASON_FACE_CAPTURE, course="某课")
    old.save(force=True)

    # 模拟新一轮运行: 重新配置全局记录簿
    configure_default_registry(path)
    chaoxing, calls = make_chaoxing()
    result = chaoxing.study_video({"courseId": "1", "title": "某课"}, job, {})

    assert result == base.StudyResult.SKIPPED, result
    assert not calls, "上次已判定做不过去, 这次不该再发上报"
    assert chaoxing.skipped_points[0]["reason_code"] == REASON_FACE_CAPTURE
    print("[ok] 跨运行记忆: 记录簿里的永久条目开播前直接跳过")


def test_no_face_capture_still_plays():
    """回归: 普通视频不受影响, 照常进入播放流程"""
    setup_base()
    configure_default_registry(os.path.join(tempfile.mkdtemp(), "skip_registry.json"))
    chaoxing, calls = make_chaoxing()

    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        chaoxing.study_video({"courseId": "1", "title": "课"},
                             {"jobid": "888", "objectid": "obj-888",
                              "name": "普通视频.mp4", "playTime": 0,
                              "videoFaceCaptureEnc": ""}, {})
    finally:
        time.sleep = real_sleep

    assert calls, "普通视频应该照常上报进度"
    print(f"[ok] 普通视频照常播放, {len(calls)} 次上报")


def test_switch_can_be_turned_off():
    """skip_face_capture=false 时照常播(用来确认是不是误判)"""
    setup_base()
    configure_default_registry(os.path.join(tempfile.mkdtemp(), "skip_registry.json"))
    chaoxing, calls = make_chaoxing()
    chaoxing.skip_face_capture = False

    real_sleep = time.sleep
    time.sleep = lambda *_: None
    try:
        chaoxing.study_video({"courseId": "1", "title": "课"},
                             {"jobid": "999", "objectid": "obj-999",
                              "name": "人脸视频.mp4", "playTime": 0,
                              "videoFaceCaptureEnc": "701f717922e30129e914bcec53d18d3f"}, {})
    finally:
        time.sleep = real_sleep

    assert calls, "--no-skip-face-capture 时应照常播放"
    print("[ok] 开关可关: --no-skip-face-capture 时照常播放")


if __name__ == "__main__":
    test_registry_persist_and_reload()
    test_retryable_reason_not_permanent()
    test_summary_lines_readable()
    test_face_capture_video_skipped_without_any_request()
    test_quiz_video_is_not_skipped_as_face_capture()
    test_legacy_face_record_is_dropped()
    test_stale_face_record_revoked_on_recheck()
    test_recorded_permanent_skips_next_run()
    test_no_face_capture_still_plays()
    test_switch_can_be_turned_off()
    print("\n全部通过")
