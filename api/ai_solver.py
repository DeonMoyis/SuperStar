# -*- coding: utf-8 -*-
"""
DeepSeek AI 搜题 / 答题模块

用户只在配置里填一个 API Key，其余（协议、提示词、重试、缓存、限流、并发）都在这里处理好。

为什么不用 answer.py 里原来的 AI 题库类：
1. 原来那道题只把「题干 + 选项」丢给模型，选择题还先把选项字母删掉，导致模型回的是
   选项内容，程序再去猜它是第几个选项，猜错就白答了；
2. 没有 JSON 输出约束，模型经常附带解释、markdown 代码块，解析失败即丢弃；
3. 没有失败重试、并发控制和缓存，几十道题就是几十次串行请求，慢且容易触发限流。

这个模块的做法：
- 用 DeepSeek 官方的 JSON Output（response_format={"type": "json_object"}）拿结构化结果；
- 选择题直接要求模型回答**选项字母**，程序只做校验，不做猜测；
- 答案不合法（字母不在选项里 / JSON 解析失败 / 返回空）时，带上「可选范围」再问一次（自动纠错）；
- 同样的题目命中本地缓存，不重复烧钱；
- 支持并发（默认 4），每道题独立重试 + 指数退避，单题失败不影响整份作业。
"""
import hashlib
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from api.logger import logger

__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_BASE_URL",
    "AIConfig",
    "AIAnswer",
    "AISolver",
    "configure_default_solver",
    "get_default_solver",
    "release_default_solver",
    "question_has_image",
]

DEFAULT_MODEL = "deepseek-flash"          # DeepSeek-V4.1-Flash-1017
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_CACHE_FILE = "ai_cache.json"

# 缓存版本号: 提示词或校验逻辑一改就 +1, 让旧缓存自动失效
# (否则前一天答错的答案会被永久缓存, 改了提示词也还是错)
CACHE_VERSION = "v2"

# 开深度思考后, 思考过程也要占输出配额, 1024 很容易被吃掉导致 content 为空,
# 所以打开 thinking 时把上限抬到这个值(用户显式填了更大的值则不动)
THINKING_MIN_TOKENS = 4096

# 置信度低于这个值的答案不写缓存: 大概率是猜的, 留着只会污染后续运行
CACHE_MIN_CONFIDENCE = 0.5

# 题干/选项里出现这些标记, 说明题目依赖图片, 而模型只能看到一串 URL
IMAGE_MARKERS = re.compile(r"<img\b|\[图片\]|!\[", re.IGNORECASE)

# 题型中文名, 只用于提示词
TYPE_CN = {
    "single": "单选题",
    "multiple": "多选题",
    "judgement": "判断题",
    "completion": "填空题",
    "shortanswer": "简答题",
    "unknown": "题目",
}

SYSTEM_PROMPT = """你是一个严谨的网课答题助手，需要根据题目和选项给出最可能正确的答案。

严格遵守以下规则：
1. 只输出一个 JSON 对象，不要输出 markdown 代码块、不要输出任何解释性段落。
2. 先在 analysis 里做推理（不超过 100 字：关键依据、为什么排除其他选项），把它写在 answer 前面，再给答案。
3. 单选题：answer 里只放一个选项字母，例如 ["B"]。
4. 多选题：answer 里放所有**有依据**的选项字母，按字母升序，例如 ["A", "C", "D"]。
   只选你能说出理由的选项；不要为了凑数硬加，宁可少选也不要多选无关项（多选错一个通常整题判错）。
5. 判断题：answer 里只放 "正确" 或 "错误"。
6. 填空题：按空的先后顺序，每个空一个元素；一空多答案用 "@@" 分隔，例如 ["光合作用", "叶绿体"]。
7. 简答题：answer 里放一段可直接提交的中文答案，控制在 200 字以内。
8. 选择题一律回答选项字母，不要输出选项内容；不要假设选项一定从 A 开始。
9. confidence 表示你对该答案的把握，取值 0 到 1；reason 用不超过 20 字说明依据。
10. 如果题目信息不全（题干里有你看不到的图片、缺少条件、选项被截断），就如实把 confidence 调低，
    不要凭空编造题干没有的条件。

输出格式示例：
{"analysis": "由 v=at 得 t=v/a=2s，故选 B", "answer": ["B"], "confidence": 0.93, "reason": "运动学公式"}"""

REPAIR_PROMPT = """你上一次的回答不符合要求：{problem}。
本题的有效选项只有 {letters}，请重新作答，只输出 JSON 对象，answer 中的字母必须来自 {letters}。"""


def question_has_image(q_info: dict[str, Any]) -> bool:
    """题目是否依赖图片

    decode.py 把题干里的 <img> 转成了 `<img src="...">` 这种纯文本,
    模型看到的只是一串 URL, 图里的图形/公式/表格它完全看不到。
    这种题的答案只能算推测, 需要标记出来让人工复核。
    """
    text = f"{q_info.get('title') or ''}\n{q_info.get('options') or ''}"
    return bool(IMAGE_MARKERS.search(text))


# ------------------------------------------------------------------ 配置

@dataclass
class AIConfig:
    """DeepSeek 相关配置, 全部有默认值, 用户正常只需要填 api_key"""
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.2
    max_tokens: int = 1024
    timeout: float = 60.0
    max_retries: int = 3
    min_interval_seconds: float = 0.0
    concurrency: int = 4
    thinking: bool = True           # 深度思考(默认开): 推理题更准, 慢一些也更费 token
    cache: bool = True
    cache_file: str = DEFAULT_CACHE_FILE
    mode: str = "fallback"          # fallback-题库优先 / priority-AI优先 / off-关闭
    check_connection: bool = True   # 启动时是否检查 Key 是否可用

    @property
    def enabled(self) -> bool:
        return self.mode != "off" and bool(self.api_key.strip())

    def __post_init__(self):
        # 直接构造 AIConfig(thinking=True) 时也要保证配额够用,
        # 否则思考 token 会把 1024 的输出配额吃光, content 变成空
        if self.thinking and self.max_tokens < THINKING_MIN_TOKENS:
            self.max_tokens = THINKING_MIN_TOKENS

    def masked_key(self) -> str:
        key = self.api_key.strip()
        if not key:
            return "<未配置>"
        if len(key) <= 10:
            return key[:2] + "***"
        return f"{key[:6]}***{key[-4:]}"


def normalize_ai_config(raw: Optional[dict[str, Any]]) -> AIConfig:
    """把 ini 里的字符串配置转成 AIConfig, 非法值一律回退默认值"""
    config = AIConfig()
    if not raw:
        return config

    def get(*names, default=""):
        for name in names:
            if name in raw and str(raw[name]).strip() != "":
                return str(raw[name]).strip()
        return default

    def as_float(value, default):
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return default

    def as_int(value, default):
        try:
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return default

    def as_bool(value, default):
        if str(value).strip() == "":
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}

    # 兼容两种写法: [ai] 里写 api_key, 也认 deepseek_key / key
    config.api_key = get("api_key", "deepseek_key", "key")
    config.base_url = get("base_url", "endpoint", default=DEFAULT_BASE_URL) or DEFAULT_BASE_URL
    config.model = get("model", default=DEFAULT_MODEL) or DEFAULT_MODEL
    config.temperature = min(2.0, max(0.0, as_float(get("temperature", default="0.2"), 0.2)))
    config.max_tokens = max(64, as_int(get("max_tokens", default="1024"), 1024))
    config.timeout = max(5.0, as_float(get("timeout", default="60"), 60.0))
    config.max_retries = max(1, as_int(get("max_retries", default="3"), 3))
    config.min_interval_seconds = max(0.0, as_float(get("min_interval_seconds", default="0"), 0.0))
    config.concurrency = min(16, max(1, as_int(get("concurrency", default="4"), 4)))
    config.thinking = as_bool(get("thinking", default="true"), True)
    if config.thinking and config.max_tokens < THINKING_MIN_TOKENS:
        # 思考 token 也算在输出配额里, 1024 会被思考吃光, 导致返回空内容
        logger.info(
            f"[AI] 已开启深度思考, max_tokens 由 {config.max_tokens} 自动提升到 {THINKING_MIN_TOKENS}"
        )
        config.max_tokens = THINKING_MIN_TOKENS
    config.cache = as_bool(get("cache", default="true"), True)
    config.cache_file = get("cache_file", default=DEFAULT_CACHE_FILE) or DEFAULT_CACHE_FILE
    config.check_connection = as_bool(get("check_connection", default="true"), True)

    mode = get("mode", default="fallback").lower()
    if mode not in {"fallback", "priority", "off"}:
        logger.warning(f'[AI] mode="{mode}" 无效, 已回退为 fallback')
        mode = "fallback"
    config.mode = mode

    if not config.api_key:
        # 没填 key 不算错误, 只是不启用 AI
        config.mode = "off"
    return config


# ------------------------------------------------------------------ 数据结构

@dataclass
class AIAnswer:
    """AI 对一道题的回答"""
    answer: list[str] = field(default_factory=list)
    confidence: float = 0.0
    reason: str = ""
    from_cache: bool = False
    repaired: bool = False
    has_image: bool = False   # 题目依赖图片, 模型看不到图, 答案只能算推测
    raw: str = ""

    @property
    def text(self) -> str:
        """拼成超星提交时用的字符串"""
        values = [str(item).strip() for item in self.answer if str(item).strip()]
        return "\n".join(values)


# ------------------------------------------------------------------ 缓存

class AICache:
    """答案缓存, 同一道题不重复问模型 (线程安全, 落盘可复用)

    namespace 用来隔离「不同模型」的答案: key 里带 model + CACHE_VERSION,
    换模型或改提示词后旧答案自动失效, 不会被历史错误答案拖累。
    """

    def __init__(self, path: str = DEFAULT_CACHE_FILE, namespace: str = ""):
        self.path = path
        self.namespace = namespace
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {}
        self._dirty = 0
        self._load()

    def _load(self):
        try:
            if os.path.isfile(self.path):
                with open(self.path, "r", encoding="utf8") as fp:
                    data = json.load(fp)
                if isinstance(data, dict):
                    self._data = data
                    logger.info(f"[AI] 已加载答案缓存 {len(data)} 条")
        except Exception as exc:
            logger.warning(f"[AI] 缓存文件读取失败, 将从空缓存开始: {exc}")
            self._data = {}

    def make_key(self, q_info: dict[str, Any]) -> str:
        raw = "|".join([
            CACHE_VERSION,
            self.namespace,
            str(q_info.get("type", "")),
            re.sub(r"\s+", "", str(q_info.get("title", ""))),
            re.sub(r"\s+", "", str(q_info.get("options", ""))),
        ])
        return hashlib.md5(raw.encode("utf8")).hexdigest()

    def get(self, q_info: dict[str, Any]) -> Optional[AIAnswer]:
        with self._lock:
            item = self._data.get(self.make_key(q_info))
        if not item:
            return None
        return AIAnswer(
            answer=list(item.get("answer") or []),
            confidence=float(item.get("confidence") or 0.0),
            reason=str(item.get("reason") or ""),
            from_cache=True,
        )

    def put(self, q_info: dict[str, Any], answer: AIAnswer):
        if not answer.answer:
            return
        with self._lock:
            self._data[self.make_key(q_info)] = {
                "answer": list(answer.answer),
                "confidence": answer.confidence,
                "reason": answer.reason,
                "title": str(q_info.get("title", ""))[:120],
            }
            self._dirty += 1
            if self._dirty >= 10:
                self.flush()

    def flush(self):
        with self._lock:
            if not self._dirty:
                return
            try:
                tmp = f"{self.path}.tmp"
                with open(tmp, "w", encoding="utf8") as fp:
                    json.dump(self._data, fp, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
                self._dirty = 0
            except Exception as exc:
                logger.warning(f"[AI] 缓存写入失败: {exc}")

    def __len__(self):
        return len(self._data)


# ------------------------------------------------------------------ 求解器

class AISolver:
    """把一道题丢给 DeepSeek, 拿回结构化答案"""

    def __init__(self, config: Optional[AIConfig] = None, client=None):
        self.config = config or AIConfig()
        self._client = client
        self._client_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0
        self._cache = (
            AICache(self.config.cache_file, namespace=self.config.model)
            if self.config.cache else None
        )

        # 运行统计
        self._stat_lock = threading.Lock()
        self.requests = 0
        self.failures = 0
        self.repairs = 0
        self.cache_hits = 0
        self.image_questions = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    # --------------------------------------------------------- 对外接口
    @property
    def available(self) -> bool:
        return self.config.enabled

    def solve(self, q_info: dict[str, Any]) -> Optional[AIAnswer]:
        """单题作答: 缓存 -> 请求 -> 校验 -> 自动纠错"""
        if not self.available:
            return None

        has_image = question_has_image(q_info)

        # 注意: 不能用 `if self._cache:` —— AICache 定义了 __len__,
        # 空缓存会被判定为 False, 导致缓存永远不生效
        if self._cache is not None:
            cached = self._cache.get(q_info)
            if cached:
                cached.has_image = has_image
                with self._stat_lock:
                    self.cache_hits += 1
                logger.info(f"[AI] 命中缓存: {q_info.get('title', '')[:40]} -> {cached.text}")
                return cached

        messages = self._build_messages(q_info, has_image=has_image)
        answer = self._ask(messages, q_info)
        if answer is None:
            return None
        answer.has_image = has_image
        if has_image:
            with self._stat_lock:
                self.image_questions += 1
            logger.warning(
                f"[AI] 图片题(模型只看到图片链接, 看不到图), 答案仅供参考: "
                f"{q_info.get('title', '')[:40]}"
            )

        problem = self._postprocess(q_info, answer)
        if problem:
            # 自动纠错: 把「可选范围」告诉模型再问一次
            with self._stat_lock:
                self.repairs += 1
            letters = self._option_letters(q_info)
            logger.warning(f"[AI] 答案不合法({problem}), 正在纠错重问: {q_info.get('title', '')[:40]}")
            repair_messages = messages + [
                {"role": "assistant", "content": answer.raw or json.dumps({"answer": answer.answer}, ensure_ascii=False)},
                {"role": "user", "content": REPAIR_PROMPT.format(problem=problem, letters="/".join(letters) or "无")},
            ]
            repaired = self._ask(repair_messages, q_info)
            if repaired is not None and not self._postprocess(q_info, repaired):
                repaired.repaired = True
                repaired.has_image = has_image
                answer = repaired
            else:
                logger.warning("[AI] 纠错后仍不合法, 本题放弃 AI 答案")
                return None

        if self._cache is not None:
            if answer.confidence and answer.confidence < CACHE_MIN_CONFIDENCE:
                # 低置信度基本等于猜的, 缓存下来只会让下一次也照着错
                logger.info(
                    f"[AI] 置信度 {answer.confidence:.2f} 偏低, 本题答案不写入缓存: "
                    f"{q_info.get('title', '')[:30]}"
                )
            else:
                self._cache.put(q_info, answer)
        return answer

    def solve_many(self, questions: list[dict[str, Any]]) -> dict[str, AIAnswer]:
        """
        批量作答 (并发)。

        返回 {题目id: AIAnswer}, 没答出来的题不会出现在结果里。
        单题异常只记日志, 不影响其他题 —— 作业/考试中途崩掉比答错更麻烦。
        """
        if not self.available or not questions:
            return {}

        results: dict[str, AIAnswer] = {}
        total = len(questions)
        logger.info(f"[AI] 开始作答 {total} 道题 (模型 {self.config.model}, 并发 {self.config.concurrency})")
        started = time.time()

        def worker(question):
            try:
                answer = self.solve(question)
            except Exception as exc:
                with self._stat_lock:
                    self.failures += 1
                logger.error(f"[AI] 题目作答异常: {type(exc).__name__}: {exc}")
                return None
            return (question.get("id", ""), answer)

        with ThreadPoolExecutor(max_workers=self.config.concurrency) as executor:
            for done, outcome in enumerate(executor.map(worker, questions), start=1):
                if not outcome:
                    continue
                question_id, answer = outcome
                if answer:
                    results[question_id] = answer
                if done % 10 == 0 or done == total:
                    logger.info(f"[AI] 进度 {done}/{total}, 已得到答案 {len(results)} 道")

        if self._cache is not None:
            self._cache.flush()
        logger.info(f"[AI] 批量作答完成: {len(results)}/{total} 道, 耗时 {time.time() - started:.1f}s, {self.stats()}")
        return results

    def check_connection(self) -> tuple[bool, str]:
        """启动时的连通性检查, 会消耗极少 token"""
        if not self.config.api_key:
            return False, "未填写 API Key"
        try:
            messages = [{"role": "user", "content": "请只回复 JSON: {\"ok\": true}"}]
            # 开深度思考时不能用 32 这种小额度, 否则思考会把配额吃光返回空内容
            content = self._request(messages, json_mode=True, max_tokens=None if self.config.thinking else 32)
        except Exception as exc:
            return False, self._explain_error(exc)

        if content and "ok" in content.lower():
            return True, f"连接正常 (模型 {self.config.model})"
        return False, "接口返回内容异常, 请检查 base_url / model 是否正确"

    def stats(self) -> str:
        extra = f", 其中图片题 {self.image_questions} 题(模型看不到图)" if self.image_questions else ""
        return (
            f"请求 {self.requests} 次, 失败 {self.failures} 次, 纠错 {self.repairs} 次, "
            f"缓存命中 {self.cache_hits} 次, tokens 输入 {self.prompt_tokens} / 输出 {self.completion_tokens}"
            f"{extra}"
        )

    # --------------------------------------------------------- 提示词
    @staticmethod
    def _option_letters(q_info: dict[str, Any]) -> list[str]:
        options = q_info.get("options") or ""
        letters = []
        for line in str(options).split("\n"):
            matched = re.match(r"^\s*([A-Za-z])", line)
            if matched:
                letters.append(matched.group(1).upper())
        return letters

    def _build_messages(self, q_info: dict[str, Any], has_image: bool = False) -> list[dict[str, str]]:
        q_type = q_info.get("type", "unknown")
        type_cn = TYPE_CN.get(q_type, "题目")
        lines = [f"题型：{type_cn}", f"题目：{q_info.get('title', '')}"]
        options = str(q_info.get("options") or "").strip()
        if options:
            lines.append("选项：\n" + options)

        if q_type == "multiple":
            letters = "/".join(self._option_letters(q_info))
            if letters:
                lines.append(f"（本题为多选，答案字母必须来自 {letters}；只选有依据的，不要凑数）")

        if has_image:
            lines.append(
                "（注意：本题题干或选项里有图片，图片内容你无法读取，"
                "只能根据文字信息推断；信息不足时把 confidence 调低，不要编造条件）"
            )
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(lines)},
        ]

    # --------------------------------------------------------- 请求
    def _get_client(self):
        if self._client is not None:
            return self._client
        with self._client_lock:
            if self._client is None:
                from openai import OpenAI
                kwargs = {
                    "api_key": self.config.api_key,
                    "base_url": self.config.base_url,
                    "timeout": self.config.timeout,
                    "max_retries": 0,  # 重试由本模块控制, 便于打日志和退避
                }
                self._client = OpenAI(**kwargs)
        return self._client

    def _throttle(self):
        interval = self.config.min_interval_seconds
        if interval <= 0:
            return
        with self._rate_lock:
            wait = interval - (time.time() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.time()

    def _request(self, messages, json_mode: bool = True, max_tokens: Optional[int] = None) -> str:
        client = self._get_client()
        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
            "max_tokens": max_tokens or self.config.max_tokens,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if not self.config.thinking:
            # 不开深度思考时显式关掉: DeepSeek 默认可能走思考档, 关掉更快更省
            # (不支持的兼容端点会忽略该字段)
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

        last_error: Optional[Exception] = None
        for attempt in range(1, self.config.max_retries + 1):
            self._throttle()
            try:
                with self._stat_lock:
                    self.requests += 1
                completion = client.chat.completions.create(**kwargs)
                self._record_usage(completion)
                content = (completion.choices[0].message.content or "").strip()
                if content:
                    return content
                # JSON 模式解析失败时, 接口会用空格填充 content
                last_error = ValueError("模型返回空内容")
                logger.warning(f"[AI] 第 {attempt}/{self.config.max_retries} 次请求返回空内容, 准备重试")
            except Exception as exc:
                last_error = exc
                message = self._explain_error(exc)
                logger.warning(f"[AI] 第 {attempt}/{self.config.max_retries} 次请求失败: {message}")
                # 鉴权/额度问题重试没意义, 直接抛出
                if self._is_fatal(exc):
                    raise
                # 深度思考 + JSON 输出在部分端点不兼容(400), 去掉 JSON 约束改用文本解析
                if self.config.thinking and "response_format" in kwargs and self._is_bad_request(exc):
                    kwargs.pop("response_format", None)
                    logger.warning("[AI] 思考模式与 JSON 输出不兼容, 本次请求改为纯文本解析")
                    continue

            if attempt < self.config.max_retries:
                backoff = min(8.0, 1.5 ** attempt)
                time.sleep(backoff)

        with self._stat_lock:
            self.failures += 1
        raise last_error or RuntimeError("AI 请求失败")

    def _record_usage(self, completion):
        usage = getattr(completion, "usage", None)
        if not usage:
            return
        with self._stat_lock:
            self.prompt_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.completion_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

    @staticmethod
    def _is_fatal(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        return status in {401, 402, 403}

    @staticmethod
    def _is_bad_request(exc: Exception) -> bool:
        status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        return status == 400

    @staticmethod
    def _explain_error(exc: Exception) -> str:
        status = getattr(exc, "status_code", None)
        text = str(exc)
        if status == 401:
            return "API Key 无效 (401)，请检查配置里的 api_key"
        if status == 402:
            return "账户余额不足 (402)，请到 DeepSeek 平台充值"
        if status == 403:
            return "没有访问权限 (403)，请检查 Key 是否可用"
        if status == 429:
            return "请求过于频繁或超出并发限制 (429)，稍后自动重试"
        if status and status >= 500:
            return f"服务端错误 ({status})，稍后自动重试"
        if "timeout" in text.lower() or "timed out" in text.lower():
            return "请求超时，可调大 timeout 或降低 concurrency"
        return text[:200]

    def _ask(self, messages, q_info: dict[str, Any]) -> Optional[AIAnswer]:
        try:
            content = self._request(messages)
        except Exception as exc:
            logger.error(f"[AI] 本题最终失败: {self._explain_error(exc)}")
            return None
        parsed = self._parse(content)
        if parsed is None:
            logger.warning(f"[AI] 无法解析模型输出: {content[:120]}")
            return None
        return parsed

    # --------------------------------------------------------- 解析与校验
    @staticmethod
    def _parse(content: str) -> Optional[AIAnswer]:
        text = (content or "").strip()
        if not text:
            return None

        # 去掉 ```json ``` 包裹
        fenced = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL)
        if fenced:
            text = fenced.group(1).strip()

        # 有些模型会在 JSON 前后加解释, 这里截取第一个完整对象
        if not text.startswith("{"):
            start, end = text.find("{"), text.rfind("}")
            if start != -1 and end > start:
                text = text[start:end + 1]

        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            # 兜底: 模型只回了一串选项字母, 例如 "B" / "A,C"
            short = re.sub(r"[\s,，、;；]", "", text)
            if re.fullmatch(r"[A-Za-z]{1,6}", short):
                return AIAnswer(answer=sorted(set(short.upper())), confidence=0.3, raw=content)
            return None

        if not isinstance(data, dict):
            if isinstance(data, list):
                data = {"answer": data}
            else:
                return None

        raw_answer = data.get("answer", data.get("Answer"))
        if raw_answer is None:
            return None
        if isinstance(raw_answer, (str, int, float)):
            # "B" 或 "A,C" 这类写法拆开
            pieces = re.split(r"[,，、;；\s]+", str(raw_answer)) if re.search(r"[,，、;；\s]", str(raw_answer)) else [str(raw_answer)]
            values = [piece for piece in pieces if piece]
        elif isinstance(raw_answer, list):
            values = [str(item) for item in raw_answer if str(item).strip()]
        else:
            return None

        try:
            confidence = float(data.get("confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = min(1.0, max(0.0, confidence if confidence <= 1 else confidence / 100))

        return AIAnswer(
            answer=values,
            confidence=confidence,
            reason=str(data.get("reason", "") or "")[:120],
            raw=content,
        )

    def _postprocess(self, q_info: dict[str, Any], answer: AIAnswer) -> Optional[str]:
        """
        把模型答案规范化成超星能用的形式, 顺便做合法性校验。

        返回 None 表示通过(此时 answer.answer 已被就地规范化), 否则返回问题描述,
        调用方会带着这个描述再问模型一次。
        """
        q_type = q_info.get("type", "unknown")
        values = [str(item).strip() for item in answer.answer if str(item).strip()]
        if not values:
            return "没有给出答案"

        if q_type in {"single", "multiple"}:
            letters, problem = self._coerce_letters(q_info, values)
            if problem:
                return problem
            if q_type == "single" and len(letters) != 1:
                return f"单选题只能有一个答案，你给了 {''.join(letters)}"
            if q_type == "multiple" and len(letters) < 1:
                return "多选题没有给出任何选项字母"
            answer.answer = sorted(set(letters))
            return None

        if q_type == "judgement":
            first = values[0]
            if self._is_true(first):
                answer.answer = ["正确"]
            elif self._is_false(first):
                answer.answer = ["错误"]
            else:
                return "判断题只能回答「正确」或「错误」"
            return None

        # 填空题/简答题: 去掉空白元素, 保持模型给的顺序
        answer.answer = values
        return None

    @staticmethod
    def _is_true(value: str) -> bool:
        return value.strip().lower() in {"正确", "对", "√", "是", "true", "t", "yes", "a"}

    @staticmethod
    def _is_false(value: str) -> bool:
        return value.strip().lower() in {"错误", "错", "×", "x", "否", "false", "f", "no", "b"}

    def _option_map(self, q_info: dict[str, Any]) -> dict[str, str]:
        """{'A': '选项内容', ...}"""
        mapping: dict[str, str] = {}
        for line in str(q_info.get("options") or "").split("\n"):
            matched = re.match(r"^\s*([A-Za-z])\s*[.、:：)）]?\s*(.*)$", line)
            if matched:
                mapping[matched.group(1).upper()] = matched.group(2).strip()
        return mapping

    def _coerce_letters(self, q_info: dict[str, Any], values: list[str]) -> tuple[list[str], Optional[str]]:
        """把模型给的东西(字母/字母串/选项内容)统一成选项字母"""
        option_map = self._option_map(q_info)
        allowed = set(option_map) or set(self._option_letters(q_info))
        if not allowed:
            # 没有选项信息(题目本身可能是填空式选择), 不做强校验
            return [value[:1].upper() for value in values], None

        letters: list[str] = []
        for value in values:
            text = value.strip()
            upper = text.upper()

            # 纯字母, 或者没加分隔符的连续字母 (如 "BD")
            if re.fullmatch(r"[A-Za-z]{1,6}", text) and all(char in allowed for char in upper):
                letters.extend(upper)
                continue

            # "B. 选项内容" 这种带前缀的
            prefixed = re.match(r"^([A-Za-z])\s*[.、:：)）]", text)
            if prefixed and prefixed.group(1).upper() in allowed:
                letters.append(prefixed.group(1).upper())
                continue

            # 模型直接回了选项内容, 反查字母
            matched_letter = self._letter_by_content(text, option_map)
            if matched_letter:
                letters.append(matched_letter)
                continue

            return [], f"答案「{text}」不是有效选项，本题可选字母只有 {'/'.join(sorted(allowed))}"

        return letters, None

    @staticmethod
    def _letter_by_content(content: str, option_map: dict[str, str]) -> str:
        def norm(text: str) -> str:
            text = re.sub(r"[\s，。！？；：,.!?;:()（）\[\]【】\"“”‘’\-_/\\|]", "", text)
            return text.lower()

        target = norm(content)
        if not target:
            return ""
        # 先精确匹配, 再包含匹配(避免长选项误伤)
        for letter, text in option_map.items():
            if norm(text) == target:
                return letter
        for letter, text in option_map.items():
            normalized = norm(text)
            if normalized and (target in normalized or normalized in target):
                return letter
        return ""


# ------------------------------------------------------------------ 全局默认求解器
# main.py 启动时用 configure_default_solver() 注入 [ai] 配置,
# answer.py 里的 DeepSeek 题库和自动答题模块都通过 get_default_solver() 复用同一个实例
# (共享缓存和限流, 不会重复建连接)。

_default_solver: Optional[AISolver] = None
_default_lock = threading.Lock()


def configure_default_solver(raw_config: Optional[dict[str, Any]]) -> AISolver:
    """根据配置初始化全局求解器, 返回实例(未填 key 时返回的是不可用实例)"""
    global _default_solver
    with _default_lock:
        config = normalize_ai_config(raw_config)
        _default_solver = AISolver(config)
        if config.enabled:
            logger.info(
                f"[AI] 已启用 DeepSeek 答题: 模型 {config.model}, 模式 {config.mode}, "
                f"深度思考 {'开' if config.thinking else '关'}, max_tokens {config.max_tokens}, "
                f"Key {config.masked_key()}"
            )
        elif raw_config:
            logger.info("[AI] 未填写 api_key, AI 答题未启用")
        return _default_solver


def get_default_solver() -> Optional[AISolver]:
    """取全局求解器, 没有配置过则返回 None"""
    return _default_solver


def release_default_solver():
    """把缓存落盘并清空全局实例(程序退出前调用)"""
    global _default_solver
    with _default_lock:
        if _default_solver and _default_solver._cache is not None:
            _default_solver._cache.flush()
        _default_solver = None
