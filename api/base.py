# -*- coding: utf-8 -*-
import functools
import os
import random
import re
import threading
import time
from difflib import SequenceMatcher
from enum import Enum, IntEnum
from hashlib import md5
from typing import Self, Optional, Literal

import requests
from loguru import logger
from requests import RequestException
from requests.adapters import HTTPAdapter
from tqdm import tqdm

from api.answer import *
from api.answer_check import cut
from api.answer_engine import AnswerEngine
from api.cipher import AESCipher
from api.config import GlobalConst as gc
from api.cookies import save_cookies, use_cookies
from api.decode import (
    decode_course_list,
    decode_course_point,
    decode_course_card,
    decode_course_folder,
    decode_questions_info,
)
from api.video_quiz import (
    dump_json,
    find_inline_questions,
    summarize_video_card,
)
from api.exceptions import MaxRetryExceeded
from api.playback import FORCED_SPEED, answer_gate, playback_gate, format_duration
from api.runtime_budget import runtime_budget


def get_timestamp():
    return str(int(time.time() * 1000))


def normalize_play_time(value, duration: float) -> float:
    """把播放进度统一成「秒」

    超星不同接口的 playTime 单位并不一致: 任务点列表里是毫秒(毫秒 -> 秒的
    除法在 study_video 里做), 而 /ananas/status 返回的可能是毫秒也可能是秒。
    这里做一次自适应: 超过总时长只可能是毫秒, 否则原样返回。

    这个单位搞错的后果很直接 —— 几百万毫秒被当成秒, 会被 min(duration, ...)
    立刻截断成"已播完", 视频几秒钟就"看完"了, 任务点自然容易漏。
    """
    try:
        value = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 0:
        return 0.0
    if duration and value > duration:
        converted = value / 1000.0
        if converted <= duration:
            return converted
    return value


class SessionManager:
    _instance = None
    _init_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if getattr(self, "_initialized", False):
            return

        with self._init_lock:
            if getattr(self, "_initialized", False):
                return
            self._session = requests.Session()
            self._session.mount("https://", HTTPAdapter(max_retries=10))
            self._session.mount("http://", HTTPAdapter(max_retries=10))
            self._session.request = functools.partial(self._session.request, timeout=5)
            # For debug purposes
            # self._session.verify=False
            self._session.headers.clear()
            self._session.headers.update(gc.HEADERS)
            self._session.cookies.update(use_cookies())
            self._initialized = True

    @classmethod
    def get_instance(cls) -> Self:
        return cls()

    @classmethod
    def get_session(cls) -> requests.Session:
        instance = cls.get_instance()
        return instance._session

    @classmethod
    def update_cookies(cls):
        cls.get_instance()._session.cookies.update(use_cookies())


class Account:
    username = None
    password = None
    last_login = None
    isSuccess = None

    def __init__(self, _username, _password):
        self.username = _username
        self.password = _password


class RateLimiter:
    def __init__(self, call_interval):
        self.last_call = time.time()
        self.lock = threading.Lock()
        self.call_interval = call_interval

    def limit_rate(self, random_time=False, random_min=0.0, random_max=1.0):
        with self.lock:
            now = time.time()
            base_wait = max(self.last_call + self.call_interval - now, 0)
            extra_wait = random.uniform(random_min, random_max) if random_time else 0
            call_wait = base_wait + extra_wait
            self.last_call = now + call_wait

        time.sleep(call_wait)


class StudyResult(Enum):
    SUCCESS = 0
    FORBIDDEN = 1  # 403
    ERROR = 2
    TIMEOUT = 3
    # 到达单次运行时长上限, 主动不开始(不是失败, 下次运行会重新检测)
    SKIPPED = 4

    def is_success(self):
        return self == StudyResult.SUCCESS

    def is_failure(self):
        # SKIPPED 属于「这次没做」而不是「做失败了」, 不能触发重试
        return self not in (StudyResult.SUCCESS, StudyResult.SKIPPED)

    def is_skipped(self):
        return self == StudyResult.SKIPPED


# 卡死保护: 有些任务点的 playTime 已经等于 duration, 但服务端 isPassed 一直是 false
# (常见于还有弹窗题/章节测验没做)。这种任务点再怎么上报都不会通过, 只会把接口打爆,
# 所以限制重试次数后明确放弃, 留给下次运行或人工处理。
STUCK_END_RETRY = 3     # 片尾仍不通过时, 额外重试几次
STALL_LIMIT = 3         # 连续多少次上报进度都没前进就判定卡死

# ===== 视频卡住诊断 =====
# 「进度推到片尾, 服务端 isPassed 仍是 false」有三种常见原因, 光看日志分不出来:
#   1. 视频中途有弹窗题(ans-videoquiz)没答 -> 卡片 JSON 里能找到题目
#   2. 该视频开启了人脸采集(videoFaceCaptureEnc 非空) -> 纯 HTTP 做不到
#   3. 服务端状态延迟或风控
# 下面这套东西的作用就是: 卡住时把「是哪种」直接写进日志。
VIDEO_DEBUG_DIR: Optional[str] = None


def configure_video_debug(path: Optional[str]) -> None:
    """开启视频诊断数据落盘(目录), 传 None 关闭"""
    global VIDEO_DEBUG_DIR
    VIDEO_DEBUG_DIR = path or None
    if VIDEO_DEBUG_DIR:
        os.makedirs(VIDEO_DEBUG_DIR, exist_ok=True)
        logger.info("[视频诊断] 原始诊断数据将保存到 {}", VIDEO_DEBUG_DIR)


def save_video_debug(name: str, data) -> Optional[str]:
    """把诊断数据写成文件, 未开启诊断时什么都不做"""
    if not VIDEO_DEBUG_DIR:
        return None
    safe = re.sub(r"[^\w\u4e00-\u9fa5.-]+", "_", str(name))[:80] or "item"
    path = os.path.join(VIDEO_DEBUG_DIR, safe)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(data if isinstance(data, str) else dump_json(data))
    except OSError as exc:
        logger.debug("[视频诊断] 写文件失败 {}: {}", path, exc)
        return None
    return path


class SignType(IntEnum):
    NORMAL = 0
    GESTURE = 3
    LOCATION = 4


class ActivityStatus(IntEnum):
    ACTIVE = 1
    INACTIVE = 2


class ActivityType(IntEnum):
    SIGNIN = 2


class Chaoxing:
    def __init__(self, account: Account = None, tiku: Tiku = None, **kwargs):
        self.account = account
        self.cipher = AESCipher()
        self.tiku = tiku
        self.kwargs = kwargs
        self.rollback_times = 0
        self.rate_limiter = RateLimiter(0.5)  # 其他接口速率限制比较松
        self.video_log_limiter = RateLimiter(2)  # 上报进度极其容易卡验证码，限制2s一次
        # 章节测验(每小节里的那份作业)的答题来源: 题库 -> 大模型 -> 随机兜底
        self.answer_engine = AnswerEngine(
            tiku,
            query_delay=kwargs.get("query_delay", 0),
            ai_solver=kwargs.get("ai_solver"),
            ai_mode=kwargs.get("ai_mode", "fallback"),
        )
        # 提交策略(auto/always/never + min_cover_rate), 与作业/考试共用同一份配置
        self.answer_policy = kwargs.get("answer_policy") or {}
        # 卡住的任务点(进度到片尾但服务端不认通过), 结束时统一汇总给用户
        self.stuck_points: list[dict] = []
        self._lock_stuck = threading.Lock()
        # 最近一次视频进度上报的响应体, 卡住时用来诊断
        self._last_video_resp: Optional[str] = None
        # 已经作答过的视频弹窗题(按题干/id 去重, 同一道题不重复烧钱)
        self._inline_seen: set[str] = set()

    def login(self, login_with_cookies=False):
        if login_with_cookies:
            logger.info("Logging in with cookies")
            SessionManager.update_cookies()
            logger.debug(f"Logged in with cookies: {SessionManager.get_instance()._session.cookies}")
            if not self._validate_cookie_session():
                logger.warning("Cookie 登录校验失败，尝试使用账号密码重新登录")
                if self.account and self.account.username and self.account.password:
                    return self.login(login_with_cookies=False)
                return {"status": False, "msg": "cookies 已失效，请更新 cookies 或提供账号密码"}
            logger.info("登录成功...")
            return {"status": True, "msg": "登录成功"}

        _session = requests.Session()
        _url = "https://passport2.chaoxing.com/fanyalogin"
        _data = {
            "fid": "-1",
            "uname": self.cipher.encrypt(self.account.username),
            "password": self.cipher.encrypt(self.account.password),
            "refer": "https%3A%2F%2Fi.chaoxing.com",
            "t": True,
            "forbidotherlogin": 0,
            "validate": "",
            "doubleFactorLogin": 0,
            "independentId": 0,
        }
        logger.trace("正在尝试登录...")
        resp = _session.post(_url, headers=gc.HEADERS, data=_data)
        if resp and resp.json()["status"] == True:
            save_cookies(_session)
            SessionManager.update_cookies()
            logger.info("登录成功...")
            return {"status": True, "msg": "登录成功"}
        else:
            return {"status": False, "msg": str(resp.json()["msg2"])}

    def _validate_cookie_session(self) -> bool:
        session = SessionManager.get_instance()._session
        if not session.cookies.get("_uid"):
            return False

        test_session = requests.Session()
        test_session.headers.update(gc.HEADERS)
        test_session.cookies.update(session.cookies.get_dict())

        try:
            resp = test_session.post(
                "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata",
                data={"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0},
                timeout=8,
            )
        except RequestException as exc:
            logger.debug("Cookie validation request failed: {}", exc)
            return False

        if resp.status_code != 200:
            return False

        if "passport2.chaoxing.com" in resp.text or "login" in resp.text.lower():
            return False

        return True

    def get_fid(self):
        _session = SessionManager.get_session()
        return _session.cookies.get("fid", 1024)

    def get_uid(self):
        s = SessionManager.get_session()
        if "_uid" in s.cookies:
            return s.cookies["_uid"]
        if "UID" in s.cookies:
            return s.cookies["UID"]
        raise ValueError("Cannot get uid !")

    def get_course_list(self):
        _session = SessionManager.get_session()
        _url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/courselistdata"
        _data = {"courseType": 1, "courseFolderId": 0, "query": "", "superstarClass": 0}
        logger.trace("正在读取所有的课程列表...")

        # 接口突然抽风, 增加headers
        # 有可能只是referer的问题
        _headers = {
            "Referer": "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction?moocDomain=https://mooc1-1.chaoxing.com/mooc-ans",
        }
        _resp = _session.post(_url, headers=_headers, data=_data)
        # logger.trace(f"原始课程列表内容:\n{_resp.text}")
        logger.info("课程列表读取完毕...")
        course_list = decode_course_list(_resp.text)

        _interaction_url = "https://mooc2-ans.chaoxing.com/mooc2-ans/visit/interaction"
        _interaction_resp = _session.get(_interaction_url)
        course_folder = decode_course_folder(_interaction_resp.text)
        for folder in course_folder:
            _data = {
                "courseType": 1,
                "courseFolderId": folder["id"],
                "query": "",
                "superstarClass": 0,
            }
            _resp = _session.post(_url, data=_data)
            course_list += decode_course_list(_resp.text)
        return course_list

    def get_activity_list(self, course: dict) -> list[dict]:
        s = SessionManager.get_session()
        url = "https://mobilelearn.chaoxing.com/v2/apis/active/student/activelist"
        params = {
            "fid": self.get_fid(),
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "showNotStartedActive": 0,
            "_": get_timestamp()
        }
        resp = s.get(url, params=params, allow_redirects=False)
        if resp.status_code != 200:
            logger.error("Failed to get activity list, return code: " + str(resp.status_code))
            logger.debug("Request url: " + resp.url)
            return []

        data = resp.json()
        if data["result"] != 1:
            logger.error("Unknown status: {} {}", data["result"], data["errorMsg"])
            logger.debug("Request url: " + resp.url)
            return []

        return data["data"]["activeList"]


    def pre_sign(self, course: dict, activity_id):
        s = SessionManager.get_session()
        params = {
            "general": 1,
            "sys": 1,
            "ls": 1,
            "appType": 15,
            "tid": '',
            "ut": 's',
            "uid": self.get_uid(),
            "activePrimaryId": activity_id,
            "courseId": course["courseId"],
            "classId": course["clazzId"],
        }
        resp = s.get('https://mobilelearn.chaoxing.com/newsign/preSign', params=params)
        resp_txt = resp.text
        logger.debug("Request url" + resp.url)
        if resp.status_code != 200:
            logger.error("Failed to get sign in, return code: " + str(resp.status_code) + "message: " + resp_txt)

        return resp_txt


    def sign_in_normal(self, course: dict, activity_id, name="", obj_id="aaa", lat=-1, lon=-1, type_=SignType.NORMAL):
        s = SessionManager.get_session()
        params = {
            "activeId": activity_id,
            "uid": self.get_uid(),
            "fid": self.get_fid(),
            "courseId": course["courseId"],
            "classId": course["clazzId"],
            "clientip": "",
            "objectId": obj_id,
            "name": name,
            "useragent": "",
            "latitude": lat,
            "longitude": lon,
            "appType": "15",
        }

        resp = s.get("https://mobilelearn.chaoxing.com/pptSign/stuSignajax", params=params)

        resp_txt = resp.text
        if resp.status_code != 200:
            logger.error("Failed to get sign in, return code: " + str(resp.status_code) + "message: " + resp_txt)

        if type_ != SignType.LOCATION:
            return resp_txt

        pattern = r"[^0-9\.]*(.+)米[^0-9\.]*"
        msg = re.match(pattern, resp_txt)
        logger.warning(f"距离签到位置 {msg}m")
        # TOD0: Implement triangulation for location signs
        return resp_txt


    def get_course_point(self, _courseid, _clazzid, _cpi):
        _session = SessionManager.get_session()
        _url = f"https://mooc2-ans.chaoxing.com/mooc2-ans/mycourse/studentcourse?courseid={_courseid}&clazzid={_clazzid}&cpi={_cpi}&ut=s"
        logger.trace("URL: " + _url)
        logger.trace("开始读取课程所有章节...")
        _resp = _session.get(_url)

        logger.trace(f"原始章节列表内容:\n{_resp.text}")
        logger.info("课程章节读取成功...")
        return decode_course_point(_resp.text)

    def get_job_list(self, course: dict, point: dict) -> tuple[list[dict], dict]:
        _session = SessionManager.get_session()
        self.rate_limiter.limit_rate()
        job_list = []
        job_info = {}
        cards_params = {
            "clazzid": course["clazzId"],
            "courseid": course["courseId"],
            "knowledgeid": point["id"],
            "ut": "s",
            "cpi": course["cpi"],
            "v": "2025-0424-1038-3",
            "mooc2": 1
        }

        # 学习界面任务卡片数, 很少有3个的, 但是对于章节解锁任务点少一个都不行, 可以从API /mooc-ans/mycourse/studentstudyAjax获取值, 或者干脆直接加, 但二者都会造成额外的请求
        for _possible_num in "0123456":

            logger.trace("开始读取章节所有任务点...")

            cards_params.update({"num": _possible_num})
            _resp = _session.get("https://mooc1.chaoxing.com/mooc-ans/knowledge/cards", params=cards_params)
            if _resp.status_code != 200:
                logger.error(f"未知错误: {_resp.status_code} 正在跳过")
                logger.error(_resp.text)
                return [], {}

            _job_list, _job_info = decode_course_card(_resp.text)
            if _job_info.get("notOpen", False):
                # 直接返回, 节省一次请求
                logger.info("该章节未开放")
                return [], _job_info

            job_list += _job_list
            job_info.update(_job_info)

        if not job_list:
            self.study_emptypage(course, point)

        logger.trace(f"原始任务点列表内容:\n{_resp.text}")
        logger.info("章节任务点读取成功...")

        return job_list, job_info

    def get_enc(self, clazzId, jobid, objectId, playingTime, duration, userid):
        return md5(
            f"[{clazzId}][{userid}][{jobid}][{objectId}][{playingTime * 1000}][d_yHJ!$pdA~5][{duration * 1000}][0_{duration}]"
            .encode()).hexdigest()

    # ------------------------------------------------------------- 视频卡住诊断
    def _capture_inline_questions(self, data) -> list[dict]:
        """进度上报的响应里若带「视频内嵌题」, 就地作答并记录下来

        超星在需要弹题的播放位置, 会在 /multimedia/log 的响应里带上题目;
        这个弹窗不答, 视频会停在原地, 服务端也不会把任务点判为通过 ——
        现象就是「进度到片尾了, isPassed 还是 false」。

        这里只负责识别 + 作答 + 留证据, 不去盲猜提交接口(猜错会污染账号状态)。
        """
        try:
            questions = find_inline_questions(data)
        except Exception as exc:  # 结构千奇百怪, 不能让它把播放流程带崩
            logger.debug("[视频弹窗题] 解析失败: {}", exc)
            return []

        fresh = []
        seen = getattr(self, "_inline_seen", None)
        if seen is None:
            seen = set()
            self._inline_seen = seen
        for q in questions:
            key = q.get("id") or (q.get("title") or "")[:80]
            if key and key not in seen:
                seen.add(key)
                fresh.append(q)
        if not fresh:
            return []

        logger.warning("[视频弹窗题] 进度响应里出现 {} 道视频内嵌题, 正在作答", len(fresh))
        report_summary = ""
        try:
            report = self.answer_engine.solve_all(fresh, title="视频弹窗题")
            report_summary = report.summary()
        except Exception as exc:
            logger.error("[视频弹窗题] 作答失败: {}: {}", type(exc).__name__, exc)

        answers = []
        for q in fresh:
            qid = q.get("id") or ""
            answers.append({
                "id": qid,
                "title": q.get("title"),
                "type": q.get("type"),
                "answer": (q.get("answerField") or {}).get(f"answer{qid}", ""),
            })
        logger.warning("[视频弹窗题] 题目与答案: {}", answers)
        save_video_debug(
            f"inline_quiz_{int(time.time())}.json",
            {"report": report_summary, "answers": answers, "raw": data},
        )
        return fresh

    def _diagnose_stuck_video(self, job: dict, status_json: Optional[dict] = None,
                              reason: str = "") -> dict:
        """把「这个视频为什么刷不过去」写进日志

        光看 isPassed=false 分不清是弹窗题、人脸采集还是风控。这里把三方面证据
        (卡片里有没有题目 / status 里有没有人脸采集 / 最近一次上报的响应) 一起打出来。
        """
        try:
            summary = summarize_video_card(job.get("raw_card"))
        except Exception as exc:
            summary = {"ok": False, "reason": f"诊断失败: {exc}"}

        status_info = {}
        if isinstance(status_json, dict):
            status_info = {
                k: status_json[k]
                for k in ("status", "duration", "dtoken", "crc", "key",
                          "videoFaceCapture", "faceCapture", "question", "questions")
                if k in status_json
            }

        last_resp = getattr(self, "_last_video_resp", None) or ""
        diag = {
            "任务点": job.get("name"),
            "jobid": job.get("jobid"),
            "objectid": job.get("objectid"),
            "判定原因": reason,
            "卡片诊断": summary,
            "status 关键字段": status_info,
            "最近一次上报响应": last_resp[:1500],
        }

        logger.error("[视频诊断] {} -> {}", job.get("name"), summary.get("reason"))
        if summary.get("question_count"):
            logger.error("[视频诊断] 卡片里发现 {} 道弹窗题, 题目预览: {}",
                         summary["question_count"],
                         [q.get("title", "")[:40] for q in summary.get("questions", [])[:3]])
        if summary.get("has_face_capture"):
            logger.error("[视频诊断] 该视频开启了人脸采集(videoFaceCaptureEnc 非空), "
                         "纯 HTTP 方案无法完成, 需要在网页端/App 里过人脸")
        if last_resp:
            logger.error("[视频诊断] 最近一次上报响应: {}", last_resp[:600])

        save_video_debug(f"stuck_{job.get('jobid') or 'unknown'}.json", diag)
        lock = getattr(self, "_lock_stuck", None)
        points = getattr(self, "stuck_points", None)
        if points is None:
            points = []
            self.stuck_points = points
        record = {
            "name": job.get("name"),
            "reason": summary.get("reason"),
            "has_face_capture": summary.get("has_face_capture", False),
            "question_count": summary.get("question_count", 0),
        }
        if lock is not None:
            with lock:
                points.append(record)
        else:
            points.append(record)
        return diag

    def video_progress_log(
            self,
            _session,
            _course,
            _job,
            _job_info,
            _dtoken,
            _duration,
            _playingTime,
            _type: str = "Video",
            _isdrag: int = 3,
            headers: Optional[dict] = None,
    ) -> tuple[bool, int]:

        if headers is None:
            logger.warning("null headers")
            headers = gc.VIDEO_HEADERS

        self.video_log_limiter.limit_rate(random_time=True, random_max=2)

        if "courseId" in _job["otherinfo"]:
            logger.error(_job["otherinfo"])
            raise RuntimeError("this is not possible")

        enc = self.get_enc(_course["clazzId"], _job["jobid"], _job["objectid"], _playingTime, _duration, self.get_uid())
        params = {
            "clazzId": _course["clazzId"],
            "playingTime": _playingTime,
            "duration": _duration,
            "clipTime": f"0_{_duration}",
            "objectId": _job["objectid"],
            "otherInfo": _job["otherinfo"],
            "courseId": _course["courseId"],
            "jobid": _job["jobid"],
            "userid": self.get_uid(),
            "isdrag": _isdrag,
            "view": "pc",
            "enc": enc,
            "dtype": _type
        }

        _url = (
            f"https://mooc1.chaoxing.com/mooc-ans/multimedia/log/a/"
            f"{_course['cpi']}/"
            f"{_dtoken}"
        )

        face_capture_enc = _job["videoFaceCaptureEnc"]
        att_duration = _job["attDuration"]
        att_duration_enc = _job["attDurationEnc"]

        if face_capture_enc:
            params["videoFaceCaptureEnc"] = face_capture_enc
        if att_duration:
            params["attDuration"] = att_duration
        if att_duration_enc:
            params["attDurationEnc"] = att_duration_enc

        rt = _job['rt']
        if not rt:
            rt_search = re.search(r"-rt_([1d])", _job['otherinfo'])
            if rt_search:
                rt_char = rt_search.group(1)
                rt = "0.9" if rt_char == "d" else "1"
                logger.trace(f"Got rt from otherinfo: {rt}")

        if rt:
            logger.trace(f"Got rt: {rt}")
            _job['rt'] = rt
            params.update({"rt": rt,
                           "_t": get_timestamp()})
            resp = _session.get(_url, params=params, headers=headers)
        else:
            logger.warning("Failed to get rt")
            for rt in [0.9, 1]:
                params.update({"rt": rt,
                               "_t": get_timestamp()})
                resp = _session.get(_url, params=params, headers=headers)
                if resp.status_code == 200:
                    logger.trace(resp.text)
                    self._last_video_resp = resp.text
                    data = resp.json()
                    self._capture_inline_questions(data)
                    return data["isPassed"], 200
                # elif resp.ok:
                #    # TODO: 处理验证码
                #    pass
                elif resp.status_code == 403:
                    logger.warning("出现403报错, 正常尝试切换rt")

                else:
                    logger.warning("未知错误 jobid={}, status_code={}, 摘要:\n{}",
                                   _job.get("jobid"),
                                   resp.status_code,
                                   resp.text[:200])
                    break

        if resp.status_code == 200:
            logger.trace(resp.text)
            self._last_video_resp = resp.text
            data = resp.json()
            self._capture_inline_questions(data)
            return data["isPassed"], 200

        elif resp.status_code == 403:
            logger.debug(
                "视频进度上报返回403, jobid={}, 摘要={}",
                _job.get("jobid"),
                resp.text[:200],
            )

            # 若出现两个rt参数都返回403的情况, 则跳过当前任务
            logger.error("出现403报错, 尝试修复无效, 正在跳过当前任务点...")
            logger.error("请求url: {}", resp.url)
            logger.error("请求头: {}", dict(_session.headers) | headers)
            return False, 403

        logger.error(f"未知错误: {resp.status_code}")
        logger.error("请求url:", resp.url)
        logger.error("请求头：", dict(_session.headers) | headers)
        return False, resp.status_code

    def _refresh_video_status(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]) \
            -> Optional[dict]:
        self.rate_limiter.limit_rate(random_time=True, random_max=0.2)
        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        info_url = (
            f"https://mooc1.chaoxing.com/ananas/status/{job['objectid']}?"
            f"k={self.get_fid()}&flag=normal"
        )
        try:
            resp = session.get(info_url, timeout=8, headers=headers)
        except RequestException as exc:
            logger.debug("刷新视频状态失败: {}", exc)
            return None

        if resp.status_code != 200:
            logger.debug("刷新视频状态返回码异常: {}" % resp.status_code)
            logger.debug(resp.text)
            return None

        try:
            data = resp.json()
        except ValueError as exc:
            logger.debug("解析视频状态响应失败: {}", exc)
            return None

        if data.get("status") == "success":
            return data

        return None

    def _recover_after_forbidden(self, session: requests.Session, job: dict, _type: Literal["Video", "Audio"]):
        SessionManager.update_cookies()
        refreshed = self._refresh_video_status(session, job, _type)
        if refreshed:
            return refreshed

        # FIXME: Temporarily disabled for multithreading support
        if False and self.account and self.account.username and self.account.password:
            login_result = self.login(login_with_cookies=False)
            if login_result.get("status"):
                SessionManager.update_cookies()
                return self._refresh_video_status(session, job, _type)
            logger.warning("账号密码登录失败: {}", login_result.get("msg"))

        return None

    def study_video(self, _course, _job, _job_info, _speed: float = 1.0,
                    _type: Literal["Video", "Audio"] = "Video") -> StudyResult:
        _session = SessionManager.get_session()

        headers = gc.VIDEO_HEADERS if _type == "Video" else gc.AUDIO_HEADERS
        _info_url = f"https://mooc1.chaoxing.com/ananas/status/{_job['objectid']}?k={self.get_fid()}&flag=normal"
        _video_info = _session.get(_info_url, headers=headers).json()

        if _video_info["status"] != "success":
            logger.error(f"Unknown status: {_video_info['status']}")
            return StudyResult.ERROR

        _dtoken = _video_info["dtoken"]

        _crc = _video_info["crc"]
        _key = _video_info["key"]

        # Time in the real world: last_iter, gc.THRESHOLD
        # Time in the video (can be scaled with the speed factor): duration, play_time, last_log_time, wait_time

        duration = int(_video_info["duration"])
        play_time = int(_job["playTime"]) // 1000
        wait_time = int(random.uniform(30, 90))

        logger.info(f"开始任务: {_job['name']}, 总时长: {duration}s, 已进行: {play_time}s")

        forbidden_retry = 0
        max_forbidden_retry = 2

        passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration, duration,
                                                _type, headers=headers, _isdrag=4)
        if passed:
            logger.info("任务瞬间完成: {}", _job['name'])
            return StudyResult.SUCCESS

        # 起点就已经在片尾(playTime >= duration)但服务端不认通过。
        # 旧实现在这种情况下会死循环: play_time == duration 让上报条件永远成立,
        # 而 play_time 又不可能再增长, 于是无 sleep 地疯狂刷接口, 直到 job 超时。
        # 这里先限次重试(服务端状态偶尔延迟), 仍然不认就明确放弃。
        if play_time >= duration:
            for attempt in range(1, STUCK_END_RETRY + 1):
                time.sleep(random.uniform(5, 10))
                passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration, duration,
                                                       _type, headers=headers, _isdrag=4)
                if passed:
                    logger.info("任务瞬间完成(第{}次重试): {}", attempt, _job['name'])
                    return StudyResult.SUCCESS
                if state not in (200, 403):
                    break
            logger.error(
                "任务点进度已到片尾但仍未通过: {} (已进行 {} / 总时长 {})。"
                "本次跳过该任务点, 不再空转。",
                _job['name'], format_duration(play_time), format_duration(duration),
            )
            self._diagnose_stuck_video(_job, _video_info, "进度到片尾但服务端不认通过")
            return StudyResult.SKIPPED

        # 倍速统一由 api/playback.py 钉死为 1.0, 外部传入的 _speed 一律忽略
        _speed = FORCED_SPEED

        # 全局播放位: 同一时刻只允许一个视频推进进度。
        # 并发播放会让真实耗时变成总时长的 1/N, 效果等同倍速, 同样有漏任务点的风险。
        # last_iter 必须在拿到播放位之后再初始化, 否则排队等待的时间会被当成
        # "视频正在播放", 进度一次性跳一大截。
        with playback_gate.hold(_job['name'], should_stop=runtime_budget.should_stop) as acquired:
            if not acquired:
                # 排队期间到达运行时长上限: 这个视频本次不做, 下次运行会重新检测
                logger.info("已达单次运行时长上限, 本次跳过该视频: {}", _job['name'])
                return StudyResult.SKIPPED

            last_iter = time.time()
            play_started_at = last_iter
            start_play_time = play_time
            last_log_time = 0
            last_progress = play_time
            stall_count = 0

            pbar = tqdm(total=duration, initial=play_time, desc=_job["name"],
                        unit_scale=True, bar_format='{l_bar}{bar}| {n_fmt}/{total_fmt}')
            try:
                while not passed:
                    # Sometimes the last request needs to be sent several times to complete the task
                    if play_time - last_log_time >= wait_time or play_time == duration:

                        passed, state = self.video_progress_log(_session, _course, _job, _job_info, _dtoken, duration,
                                                                int(play_time), _type, headers=headers)

                        if state == 403:
                            if forbidden_retry >= max_forbidden_retry:
                                logger.warning("403重试失败, 跳过当前任务")
                                return StudyResult.FORBIDDEN
                            forbidden_retry += 1
                            logger.warning(
                                "出现403报错, 正在尝试刷新会话状态 (第{}次)",
                                forbidden_retry,
                            )
                            time.sleep(random.uniform(2, 4))
                            refreshed_meta = self._recover_after_forbidden(_session, _job, _type)
                            if refreshed_meta:
                                _dtoken = refreshed_meta.get("dtoken", _dtoken)
                                # 刷新后时长可能变化, 用新值覆盖, 否则会用旧时长去判完成
                                new_duration = refreshed_meta.get("duration")
                                if new_duration:
                                    duration = int(new_duration)
                                play_time = normalize_play_time(
                                    refreshed_meta.get("playTime", play_time), duration
                                )
                                play_time = min(play_time, duration)

                                logger.debug("Refreshed token: {}, duration: {}, play time: {}", _dtoken, duration, play_time)
                                continue

                        elif not passed and state != 200:
                            return StudyResult.ERROR

                        # 卡死检测: 进度一点没动、服务端也一直不认通过, 说明这个任务点
                        # 靠上报推不动了(弹窗题未做/服务端状态异常), 继续刷只是打爆接口。
                        if not passed:
                            if int(play_time) > int(last_progress):
                                last_progress = play_time
                                stall_count = 0
                            else:
                                stall_count += 1
                                if stall_count >= STALL_LIMIT:
                                    logger.error(
                                        "任务点连续 {} 次上报进度都没有前进, 判定卡死: {} "
                                        "(进度 {} / 总时长 {})。本次跳过。",
                                        stall_count, _job['name'],
                                        format_duration(play_time), format_duration(duration),
                                    )
                                    self._diagnose_stuck_video(
                                        _job, _video_info,
                                        f"连续 {stall_count} 次上报进度无变化",
                                    )
                                    return StudyResult.SKIPPED

                        wait_time = int(random.uniform(30, 90))
                        last_log_time = play_time

                        logger.trace("Progress logged")

                    # Uploading the progress takes time, we assume that the video is still playing in the background, this manually calculates the time elapsed
                    dt = (time.time() - last_iter) * _speed
                    last_iter = time.time()
                    play_time = min(duration, play_time + dt)

                    pbar.n = int(play_time)
                    pbar.refresh()
                    # 必须 sleep: 旧实现在 play_time == duration 时直接跳过 sleep,
                    # 一旦服务端不认通过就是无间隔的死循环, 会把接口刷到风控。
                    # 这里统一按 THRESHOLD 走, 卡死的情况交给上面的连续无进展检测收尾。
                    time.sleep(gc.THRESHOLD)
            finally:
                pbar.close()

            wall_clock = time.time() - play_started_at
            logger.info(
                "任务完成: {} | 视频时长 {} | 起始进度 {} | 本次实际耗时 {}",
                _job['name'], format_duration(duration), format_duration(start_play_time),
                format_duration(wall_clock),
            )
            # 实际耗时明显短于待播时长时给出提示, 方便定位是"起点已有进度"还是"服务端提前放行"
            remaining = max(0, duration - start_play_time)
            if remaining > 60 and wall_clock < remaining * 0.5:
                logger.warning(
                    "本次耗时 {} 明显短于待播时长 {}, 若任务点未计入请检查是否有异常放行",
                    format_duration(wall_clock), format_duration(remaining),
                )

        return StudyResult.SUCCESS

    def study_document(self, _course, _job) -> StudyResult:
        """
        Study a document in Chaoxing platform.

        This method makes a GET request to fetch document information for a given course and job.

        Args:
            _course (dict): Dictionary containing course information with keys:
                - courseId: ID of the course
                - clazzId: ID of the class
            _job (dict): Dictionary containing job information with keys:
                - jobid: ID of the job
                - otherinfo: String containing node information
                - jtoken: Authentication token for the job

        Returns:
            requests.Response: Response object from the GET request

        Note:
            This method requires the following helper functions:
            - init_session(): To initialize a new session
            - get_timestamp(): To get current timestamp
            - re module for regular expression matching
        """
        _session = SessionManager.get_session()
        _url = f"https://mooc1.chaoxing.com/ananas/job/document?jobid={_job['jobid']}&knowledgeid={re.findall(r'nodeId_(.*?)-', _job['otherinfo'])[0]}&courseid={_course['courseId']}&clazzid={_course['clazzId']}&jtoken={_job['jtoken']}&_dc={get_timestamp()}"
        _resp = _session.get(_url)
        if _resp.status_code != 200:
            return StudyResult.ERROR
        else:
            return StudyResult.SUCCESS

    def _answer_available(self) -> bool:
        """章节测验能不能真正答题: 至少要有一个答案来源(题库或大模型)

        旧实现只看题库, 没配题库就静默 return SUCCESS, 于是"每小节一份作业"
        一份都不会做, 日志里也看不出端倪。
        """
        engine = getattr(self, "answer_engine", None)
        return bool(engine and engine.has_answer_source())

    def _should_submit(self, cover_rate: float) -> bool:
        """章节测验: 按 [answer] submit_policy 决定提交还是只保存

        auto 模式下覆盖率达标(或此前回滚过一次)才提交; 阈值取min_cover_rate。
        """
        policy = self.answer_policy or {}
        submit_policy = str(policy.get("submit_policy") or "auto").lower()
        try:
            min_cover = float(policy.get("min_cover_rate") or 0.9)
        except (TypeError, ValueError):
            min_cover = 0.9

        if submit_policy == "always":
            return True
        if submit_policy == "never":
            return False
        return cover_rate >= min_cover * 100 or self.rollback_times >= 1

    def study_work(self, _course, _job, _job_info) -> StudyResult:
        """章节测验入口: 同一时刻只允许一个测验在作答/提交

        章节内的任务点是由 5 个线程并发处理的, 一个章节里有多个测验时,
        旧实现会同时抓题、同时发 AI 请求、同时 POST 提交, 容易触发风控。
        这里和视频一样加一个全局作答位, 互相排队。
        """
        if not self._answer_available():
            logger.warning(
                "章节测验跳过: 既没有可用题库, 也没有可用的 AI 答题 ({} )",
                _job.get("name") or "未命名测验",
            )
            return StudyResult.SUCCESS

        name = _job.get("name") or "章节测验"
        with answer_gate.hold(name, should_stop=runtime_budget.should_stop) as acquired:
            if not acquired:
                return StudyResult.SKIPPED
            return self._study_work_locked(_course, _job, _job_info)

    def _study_work_locked(self, _course, _job, _job_info) -> StudyResult:
        # FIXME: 这一块可以单独搞一个类出来了，方法里面又套方法，每一次调用都会创建新的方法，十分浪费
        if not self._answer_available():
            return StudyResult.SUCCESS
        _ORIGIN_HTML_CONTENT = ""  # 用于配合输出网页源码, 帮助修复#391错误

        # FIXME: Use tenacity for retrying
        def with_retry(max_retries=3, delay=1):
            def decorator(func):
                def wrapper(*args, **kwargs):
                    retries = 0
                    while retries < max_retries:
                        try:
                            _resp = func(*args, **kwargs)

                            # 未创建完成该测验则不进行答题，目前遇到的情况是未创建完成等同于没题目
                            if '教师未创建完成该测验' in _resp.text:
                                raise PermissionError("教师未创建完成该测验")

                            questions = decode_questions_info(_resp.text)

                            if _resp.status_code == 200 and questions.get("questions"):
                                return (_resp, questions)

                            logger.warning(
                                f"无效响应 (Code: {getattr(_resp, 'status_code', 'Unknown')}), 重试中... ({retries + 1}/{max_retries})")

                        except requests.exceptions.RequestException as e:
                            logger.warning(f"请求失败: {str(e)[:50]}, 重试中... ({retries + 1}/{max_retries})")
                        retries += 1
                        time.sleep(delay * (2 ** retries))
                    raise MaxRetryExceeded(f"超过最大重试次数 ({max_retries})")

                return wrapper

            return decorator

        # 学习通这里根据参数差异能重定向至两个不同接口, 需要定向至https://mooc1.chaoxing.com/mooc-ans/workHandle/handle
        _session = SessionManager.get_session()

        _url = "https://mooc1.chaoxing.com/mooc-ans/api/work"

        @with_retry(max_retries=3, delay=1)
        def fetch_response():
            return _session.get(
                _url,
                params={
                    "api": "1",
                    "workId": _job["jobid"].replace("work-", ""),
                    "jobid": _job["jobid"],
                    "originJobId": _job["jobid"],
                    "needRedirect": "true",
                    "skipHeader": "true",
                    "knowledgeid": str(_job_info["knowledgeid"]),
                    "ktoken": _job_info["ktoken"],
                    "cpi": _job_info["cpi"],
                    "ut": "s",
                    "clazzId": _course["clazzId"],
                    "type": "",
                    "enc": _job["enc"],
                    "mooc2": "1",
                    "courseid": _course["courseId"],
                }
            )

        final_resp = {}
        questions = {}

        try:
            final_resp, questions = fetch_response()
        except Exception as e:
            logger.error(f"请求失败: {e}")
            return StudyResult.ERROR

        _ORIGIN_HTML_CONTENT = final_resp.text  # 用于配合输出网页源码, 帮助修复#391错误

        # 作答题: 统一交给 AnswerEngine
        # 顺序是 题库命中 -> 大模型 -> 选项相似度 -> 随机兜底。
        # 旧实现只查题库, 没配 [tiku] 就整段跳过, 于是「每小节一份作业」一份都不会做,
        # 而且日志里只有一句"未找到tiku配置", 很难看出是这里被跳过了。
        total_questions = len(questions["questions"])
        found_answers = 0
        report = self.answer_engine.solve_all(
            questions["questions"], title=_job.get("name") or "章节测验"
        )
        for solved in report.solved:
            q = solved.question
            # cover = 有可靠来源的答案(题库或大模型); 下面拼提交表单时只有 cover 才会真的提交
            if solved.source in ("tiku", "ai"):
                q[f'answerSource{q["id"]}'] = "cover"
                found_answers += 1
            else:
                q[f'answerSource{q["id"]}'] = "random"
        cover_rate = (found_answers / total_questions) * 100 if total_questions else 0.0
        logger.info(f"章节检测答题覆盖率： {cover_rate:.0f}% (题库 + 大模型)")
        # 提交策略与大模型下的作业/考试保持一致, 不再只看 [tiku] submit:
        #   auto   - 覆盖率达标才提交
        #   always - 直接提交
        #   never  - 只保存草稿, 留给你自己检查
        # pyFlag="" 表示提交, pyFlag="1" 表示只保存不提交。
        submit = self._should_submit(cover_rate)
        questions["pyFlag"] = "" if submit else "1"
        if submit:
            logger.info(f"章节测验提交 (覆盖率 {cover_rate:.0f}%)")
        else:
            logger.info(f"章节测验只保存不提交 (覆盖率 {cover_rate:.0f}%)")
        # 组建提交表单
        if questions["pyFlag"] == "1":
            for q in questions["questions"]:
                questions.update(
                    {
                        f'answer{q["id"]}':
                            q["answerField"][f'answer{q["id"]}'] if q[f'answerSource{q["id"]}'] == "cover" else '',
                        f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                    }
                )
        else:
            for q in questions["questions"]:
                questions.update(
                    {
                        f'answer{q["id"]}': q["answerField"][f'answer{q["id"]}'],
                        f'answertype{q["id"]}': q["answerField"][f'answertype{q["id"]}'],
                    }
                )

        del questions["questions"]

        res = _session.post(
            "https://mooc1.chaoxing.com/mooc-ans/work/addStudentWorkNew",
            data=questions,
            headers={
                "Host": "mooc1.chaoxing.com",
                "sec-ch-ua-platform": '"Windows"',
                "X-Requested-With": "XMLHttpRequest",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "sec-ch-ua-mobile": "?0",
                "Origin": "https://mooc1.chaoxing.com",
                "Sec-Fetch-Site": "same-origin",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Dest": "empty",
                # "Referer": "https://mooc1.chaoxing.com/mooc-ans/work/doHomeWorkNew?courseId=246831735&workAnswerId=52680423&workId=37778125&api=1&knowledgeid=913820156&classId=107515845&oldWorkId=07647c38d8de4c648a9277c5bed7075a&jobid=work-07647c38d8de4c648a9277c5bed7075a&type=&isphone=false&submit=false&enc=1d826aab06d44a1198fc983ed3d243b1&cpi=338350298&mooc2=1&skipHeader=true&originJobId=work-07647c38d8de4c648a9277c5bed7075a",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,en-GB;q=0.7,en-US;q=0.6,ja;q=0.5",
            },
        )
        if res.status_code == 200:
            res_json = res.json()
            if res_json["status"]:
                logger.info(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题成功 -> {res_json["msg"]}')
            else:
                logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res_json["msg"]}')
                return StudyResult.ERROR
        else:
            logger.error(f'{"提交" if questions["pyFlag"] == "" else "保存"}答题失败 -> {res.text}')
            return StudyResult.ERROR
        return StudyResult.SUCCESS

    def study_read(self, _course, _job, _job_info) -> StudyResult:
        """
        阅读任务学习, 仅完成任务点, 并不增长时长
        """
        _session = SessionManager.get_session()
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/ananas/job/readv2",
            params={
                "jobid": _job["jobid"],
                "knowledgeid": _job_info["knowledgeid"],
                "jtoken": _job["jtoken"],
                "courseid": _course["courseId"],
                "clazzid": _course["clazzId"],
            },
        )
        if _resp.status_code != 200:
            logger.error(f"阅读任务学习失败 -> [{_resp.status_code}]{_resp.text}")
            return StudyResult.ERROR
        else:
            _resp_json = _resp.json()
            logger.info(f"阅读任务学习 -> {_resp_json['msg']}")
            return StudyResult.SUCCESS

    def study_emptypage(self, _course, point):
        _session = SessionManager.get_session()
        # &cpi=0&verificationcode=&mooc2=1&microTopicId=0&editorPreview=0
        _resp = _session.get(
            url="https://mooc1.chaoxing.com/mooc-ans/mycourse/studentstudyAjax",
            params={
                "courseId": _course["courseId"],
                "clazzid": _course["clazzId"],
                "chapterId": point["id"],
                "cpi": _course["cpi"],
                "verificationcode": "",
                "mooc2": 1,
                "microTopicId": 0,
                "editorPreview": 0,
            },
        )
        if _resp.status_code != 200:
            logger.error(f"空页面任务失败 -> [{_resp.status_code}]{point['title']}")
            return StudyResult.ERROR
        else:
            logger.info(f"空页面任务完成 -> {point['title']}")
            return StudyResult.SUCCESS
