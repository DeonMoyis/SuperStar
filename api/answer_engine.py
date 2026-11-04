# -*- coding: utf-8 -*-
"""
通用答题引擎 (AnswerEngine)

把「一道题 -> 一个答案」的流程从 base.py 里抽出来, 供章节测验以外的场景复用
(作业、考试、以后可能的随堂练习)。

设计原则:
1. 只吃 api.decode.decode_questions_info() 解出来的题目结构, 不碰任何网络请求,
   这样任何页面只要能被 decode 解析, 就能用它来作答;
2. 逐级降级: 题库命中 -> 选项字母匹配 -> 相似度兜底 -> 随机兜底,
   任何一步失败都不会抛异常打断整个流程;
3. 统计覆盖率, 由调用方决定「提交」还是「只保存」, 与现有 tiku 提交逻辑保持一致。

题目结构 (由 decode.py 产出):
    {
        "id": "123456",
        "title": "题干...",
        "options": "A.xx\nB.yy\nC.zz",   # 选择题才有, 用 \n 分隔
        "type": "single" | "multiple" | "judgement" | "completion" | "shortanswer",
        "answerField": {"answer123456": "", "answertype123456": "0"},
    }
"""
import random
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Optional

from api.logger import logger

__all__ = ["SolvedQuestion", "AnswerReport", "AnswerEngine", "build_submit_payload"]

# 多选题答案/选项的切割符, 与 answer_check.cut 保持一致
_CUT_CHARS = [
    "\n", ",", "，", "|", "\r", "\t", "#", "*", "-", "_",
    "+", "@", "~", "/", "\\", ".", "&", " ", "、",
]

# 常见异体字归一, 降低「风/⻛」这类差异导致的匹配失败
_CHAR_MAP = str.maketrans({
    "⻛": "风",
    "⻔": "门",
    "⻋": "车",
    "⻢": "马",
})


@dataclass
class SolvedQuestion:
    """单题的作答结果"""
    question: dict[str, Any]
    answer: str
    source: str  # tiku(题库命中) / ai(大模型作答) / random(随机兜底) / empty(无法作答)
    needs_review: bool = False  # 题目依赖图片, 模型看不到图, 答案建议人工复核

    @property
    def is_covered(self) -> bool:
        return self.source == "tiku"

    @property
    def is_ai(self) -> bool:
        return self.source == "ai"


@dataclass
class AnswerReport:
    """一份作业/测验的整体作答报告"""
    title: str = ""
    solved: list[SolvedQuestion] = field(default_factory=list)
    elapsed: float = 0.0
    submitted: bool = False   # 是否已正式提交
    saved: bool = False       # 是否只保存了草稿
    submitted_ok: bool = True # 提交接口是否返回成功

    @property
    def total(self) -> int:
        return len(self.solved)

    @property
    def covered(self) -> int:
        """题库命中数"""
        return sum(1 for item in self.solved if item.is_covered)

    @property
    def ai_count(self) -> int:
        """大模型作答数"""
        return sum(1 for item in self.solved if item.is_ai)

    @property
    def review_count(self) -> int:
        """需要人工复核的题数(图片题: 模型只看到图片链接)"""
        return sum(1 for item in self.solved if item.needs_review)

    @property
    def cover_rate(self) -> float:
        """有依据的作答比例(题库 + 大模型), 0.0 ~ 1.0"""
        if not self.total:
            return 0.0
        return (self.covered + self.ai_count) / self.total

    def summary(self) -> str:
        review = f", 图片题 {self.review_count} 题需人工复核" if self.review_count else ""
        return (
            f"{self.title or '本次作答'}: 共 {self.total} 题, "
            f"题库命中 {self.covered} 题, AI 作答 {self.ai_count} 题, "
            f"覆盖率 {self.cover_rate * 100:.0f}%, 耗时 {self.elapsed:.1f}s{review}"
        )


class AnswerEngine:
    """题目 -> 答案 的通用引擎

    ai_mode:
        - "fallback": 先题库, 题库没命中再问大模型 (默认, 最省钱)
        - "priority": 先问大模型, 失败再回落到题库
        - "off"     : 不用大模型
    """

    def __init__(self, tiku, query_delay: float = 0.0, ai_solver=None, ai_mode: str = "fallback"):
        self.tiku = tiku
        self.query_delay = max(0.0, float(query_delay or 0))
        self.ai_solver = ai_solver if getattr(ai_solver, "available", False) else None
        self.ai_mode = ai_mode if self.ai_solver else "off"
        if ai_solver is not None and self.ai_solver is None:
            logger.warning("[AI] 已提供大模型配置但未生效(缺少 api_key 或 mode=off)")

    # ------------------------------------------------------------------ 对外接口
    def solve_all(self, questions: list[dict[str, Any]], title: str = "") -> AnswerReport:
        """批量作答, 结果直接写回每道题的 answerField"""
        started = time.time()
        report = AnswerReport(title=title)

        # AI 优先模式: 先把所有题目丢给大模型并发作答, 再逐题回填, 避免串行等待
        ai_answers: dict[str, Any] = {}
        if self.ai_mode == "priority" and questions:
            ai_answers = self.ai_solver.solve_many(questions)

        for question in questions:
            solved = self.solve_one(question, ai_prefetched=ai_answers.get(question.get("id", "")))
            report.solved.append(solved)

        report.elapsed = time.time() - started
        logger.info(report.summary())
        return report

    def solve_one(self, question: dict[str, Any], ai_prefetched=None) -> SolvedQuestion:
        """作答题, 返回的答案同时写入 question['answerField']"""
        q_type = question.get("type", "unknown")
        options = question.get("options", "") or ""
        question_id = question.get("id", "")

        answer = ""
        source = ""

        if self.ai_mode == "priority":
            answer, source = self._solve_by_ai(question, ai_prefetched)
            if not answer:
                answer, source = self._solve_by_tiku(question, q_type, options)

        elif self.ai_mode == "fallback":
            answer, source = self._solve_by_tiku(question, q_type, options)
            if not answer:
                answer, source = self._solve_by_ai(question, ai_prefetched)

        else:
            answer, source = self._solve_by_tiku(question, q_type, options)

        if not answer:
            answer = self._random_answer(q_type, options)
            source = "random"

        if not answer:
            source = "empty"
            logger.warning(f"题目 {question_id} 无法生成任何答案, 提交时留空")

        # 写回答题结构, 供 build_submit_payload 拼表单
        answer_field = question.setdefault("answerField", {})
        answer_field[f"answer{question_id}"] = answer
        question[f"answer_source{question_id}"] = source
        logger.info(f'{question.get("title", "")} 填写答案为 {answer or "<空>"} (来源: {source})')
        return SolvedQuestion(
            question=question,
            answer=answer,
            source=source,
            needs_review=bool(question.get("needs_review")),
        )

    # ------------------------------------------------------------------ 两种答案来源
    def _solve_by_tiku(self, question: dict[str, Any], q_type: str, options: str) -> tuple[str, str]:
        if self._tiku_disabled():
            return "", ""
        if self.query_delay:
            time.sleep(self.query_delay)
        try:
            resolved = self.tiku.query(question) or ""
        except Exception as exc:  # 题库挂了不能拖垮整份作业
            logger.warning(f"题库查询异常, 本题改用大模型/随机答案: {exc}")
            return "", ""
        if not resolved:
            return "", ""

        answer = self._match_answer(q_type, resolved, options)
        if not answer:
            logger.warning(f"题库有返回但没能对上选项 -> {resolved!r}")
            return "", ""
        logger.info(f"题库命中: {answer}")
        return answer, "tiku"

    def _solve_by_ai(self, question: dict[str, Any], prefetched=None) -> tuple[str, str]:
        if not self.ai_solver:
            return "", ""
        try:
            ai_answer = prefetched if prefetched is not None else self.ai_solver.solve(question)
        except Exception as exc:
            logger.error(f"[AI] 本题作答异常: {type(exc).__name__}: {exc}")
            return "", ""
        if not ai_answer:
            return "", ""

        text = self._format_ai_answer(question.get("type", "unknown"), ai_answer.answer)
        if not text:
            logger.warning("[AI] 答案为空, 放弃使用")
            return "", ""
        if getattr(ai_answer, "has_image", False):
            # 图片题: 模型只看得到图片链接, 答案只能算推测, 标记出来供收尾汇总
            question["needs_review"] = True
            logger.warning(f"[AI] 图片题答案仅供参考, 建议人工复核: {question.get('title', '')[:40]}")
        logger.info(
            f"[AI] 作答: {text} (置信度 {ai_answer.confidence:.2f}"
            f"{', 缓存' if ai_answer.from_cache else ''}{', 纠错过' if ai_answer.repaired else ''}"
            f"{', 依据: ' + ai_answer.reason if ai_answer.reason else ''})"
        )
        return text, "ai"

    @staticmethod
    def _format_ai_answer(q_type: str, values) -> str:
        """把 AI 给的结构化答案转成超星提交格式"""
        items = [str(item).strip() for item in (values or []) if str(item).strip()]
        if not items:
            return ""
        if q_type in {"single", "multiple"}:
            return "".join(sorted({item.upper()[:1] for item in items}))
        if q_type == "judgement":
            first = items[0]
            if first in {"正确", "对", "√", "是", "true", "True"}:
                return "true"
            if first in {"错误", "错", "×", "否", "false", "False"}:
                return "false"
            return ""
        # 填空题多个空用换行分隔, 简答题直接给文本
        return "\n".join(items)

    # ------------------------------------------------------------------ 答案匹配
    def _match_answer(self, q_type: str, resolved: str, options: str) -> str:
        """把题库返回的答案文本, 匹配成超星需要的提交格式"""
        if q_type == "multiple":
            return self._match_multiple(resolved, options)
        if q_type == "single":
            return self._match_single(resolved, options)
        if q_type == "judgement":
            verdict = None
            if self.tiku is not None and hasattr(self.tiku, "judgement_select"):
                verdict = self.tiku.judgement_select(resolved)
            if verdict is None:
                verdict = str(resolved).strip() in {"正确", "对", "√", "是", "true", "True"}
            return "true" if verdict else "false"
        if q_type == "completion":
            if isinstance(resolved, list):
                return "".join(str(item) for item in resolved)
            return str(resolved).strip()
        # 简答题等其他题型直接原样提交
        return str(resolved).strip()

    def _match_multiple(self, resolved: str, options: str) -> str:
        option_list = self._cut(options)
        answer_list = self._cut(resolved)
        if not option_list or not answer_list:
            return ""

        letters = ""
        for candidate in self._clean_res(answer_list):
            matched = False
            for option in option_list:
                # 去掉各种符号和前缀 ABCD 之后, 答案应当是选项的子序列
                if self._is_subsequence(candidate, option):
                    letters += option[:1]
                    matched = True
                    break
            if not matched:
                best = self._best_option_by_similarity(candidate, option_list, threshold=0.8)
                if best:
                    letters += best
        # 多选题必须排序去重, 否则提交会失败
        return "".join(sorted(set(letters)))

    def _match_single(self, resolved: str, options: str) -> str:
        option_list = self._cut(options)
        if not option_list:
            return ""

        cleaned = self._clean_res(resolved)
        if not cleaned:
            return ""

        first = cleaned[0]
        for option in option_list:
            if self._is_subsequence(first, option):
                return option[:1]
        return self._best_option_by_similarity(first, option_list, threshold=0.8)

    # ------------------------------------------------------------------ 随机兜底
    def _random_answer(self, q_type: str, options: str) -> str:
        if q_type == "multiple":
            option_list = self._cut(options)
            if not option_list:
                return ""
            select_count = self._random_multiple_count(len(option_list))
            picked = random.sample(option_list, select_count)
            return "".join(sorted(option[:1] for option in picked))

        if q_type == "single":
            option_list = self._cut(options)
            if not option_list:
                return ""
            return random.choice(option_list)[:1]

        if q_type == "judgement":
            return "true" if random.choice([True, False]) else "false"

        # 填空/简答无法随机, 留空交给调用方决定
        return ""

    @staticmethod
    def _random_multiple_count(option_count: int) -> int:
        """多选题随机选题数, 越少选项越倾向全选"""
        if option_count <= 1:
            return option_count
        max_possible = min(4, option_count)
        min_possible = min(2, option_count)
        weights_map = {
            2: [1.0],
            3: [0.3, 0.7],
            4: [0.1, 0.5, 0.4],
        }
        weights = weights_map.get(max_possible, [0.3, 0.4, 0.3])
        possible_counts = list(range(min_possible, max_possible + 1))
        weights = weights[: len(possible_counts)]
        total = sum(weights)
        if total <= 0:
            return min_possible
        weights = [weight / total for weight in weights]
        return random.choices(possible_counts, weights=weights, k=1)[0]

    # ------------------------------------------------------------------ 文本工具
    @staticmethod
    def _cut(text: str) -> Optional[list[str]]:
        if text is None:
            return None
        text = str(text)
        for char in _CUT_CHARS:
            if char not in text:
                continue
            parts = [item.strip() for item in text.split(char) if item.strip()]
            if parts:
                return parts
        stripped = text.strip()
        return [stripped] if stripped else None

    @staticmethod
    def _clean_res(values) -> list[str]:
        if isinstance(values, str):
            values = [values]
        cleaned = []
        for item in values or []:
            if len(item) > 1:
                # 只在长度 > 1 时去掉开头的字母编号, 避免误删「A」这种单字母答案
                item = re.sub(r"^[A-Za-z]\s*[.、:：)?）]?\s*|[.,!?;:，。！？；：]", "", item)
            cleaned.append(item.strip())
        return [item for item in cleaned if item]

    @staticmethod
    def _normalize(text: str) -> str:
        if not isinstance(text, str):
            text = str(text)
        normalized = text.translate(_CHAR_MAP)
        normalized = re.sub(r"^[A-Za-z]\s*[.、:：)?）]?\s*", "", normalized)
        normalized = re.sub(r"\s+", "", normalized)
        normalized = re.sub(r'[，。！？；：,.!?;:()（）\[\]【】"“”‘’\-_/\\|]', "", normalized)
        return normalized.lower()

    @classmethod
    def _option_text(cls, option: str) -> str:
        return re.sub(r"^[A-Za-z]\s*[.、:：)?）]?\s*", "", option).strip()

    @classmethod
    def _best_option_by_similarity(cls, target: str, options: list[str], threshold: float = 0.8) -> str:
        if not target or not options:
            return ""
        target_norm = cls._normalize(target)
        if not target_norm:
            return ""

        best_letter = ""
        best_score = 0.0
        for option in options:
            option_norm = cls._normalize(cls._option_text(option))
            if not option_norm:
                continue
            score = SequenceMatcher(None, target_norm, option_norm).ratio()
            if score > best_score:
                best_score = score
                best_letter = option[:1]

        if best_score >= threshold:
            logger.info(f"相似度兜底匹配成功: {best_letter} (score={best_score:.2f})")
            return best_letter
        return ""

    @staticmethod
    def _is_subsequence(needle: str, haystack: str) -> bool:
        iterator = iter(haystack)
        return all(char in iterator for char in needle)

    def _tiku_disabled(self) -> bool:
        return self.tiku is None or getattr(self.tiku, "DISABLE", False)

    def has_answer_source(self) -> bool:
        """是否至少有一个能给出答案的来源(题库或大模型)

        两者都没有时只能随机作答, 对章节测验这种要计入成绩的场景没有意义,
        调用方应该直接跳过, 而不是答一串随机答案再提交。
        """
        return (not self._tiku_disabled()) or bool(self.ai_solver)


def build_submit_payload(questions: dict[str, Any], report: AnswerReport) -> dict[str, Any]:
    """
    把 decode_questions_info() 的解析结果 + 作答结果, 摊平成可以直接 POST 的表单字段。

    与 base.py 中章节测验的提交逻辑保持一致:
    - pyFlag == "1" 表示「保存不提交」, 此时未命中的题目答案留空, 避免污染草稿;
    - pyFlag == "" 表示「正式提交」。
    """
    payload = {key: value for key, value in questions.items() if key != "questions"}
    # 页面上抓到的隐藏字段里可能有 pyFlag, 这里以调用方设置的为准
    py_flag = str(payload.get("pyFlag", "") or "")
    submit_mode = py_flag != "1"

    for item in report.solved:
        question = item.question
        question_id = question["id"]
        answer_field = question.get("answerField", {})
        answer_value = answer_field.get(f"answer{question_id}", "")
        if not submit_mode and item.source not in {"tiku", "ai"}:
            # 保存模式: 题库命中和 AI 作答都保留, 纯随机答案不写进草稿
            answer_value = ""
        payload[f"answer{question_id}"] = answer_value
        payload[f"answertype{question_id}"] = answer_field.get(f"answertype{question_id}", "")

    return payload
