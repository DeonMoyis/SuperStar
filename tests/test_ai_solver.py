# -*- coding: utf-8 -*-
"""
DeepSeek AI 答题 离线自测

不需要真的 API Key, 用一个假的 OpenAI 客户端验证:
1. JSON 解析（含 markdown 代码块包裹、纯字母兜底）
2. 答案规范化（选项内容反查字母、连续字母拆开）
3. 非法答案自动纠错重问
4. 缓存命中不重复请求
5. 鉴权失败不重试、返回空内容自动重试
6. 与答题引擎联动（source=ai、判断/填空格式）

运行方式 (在仓库根目录):
    python tests/test_ai_solver.py
"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.ai_solver import AICache, AIConfig, AISolver, normalize_ai_config
from api.answer_engine import AnswerEngine


# ------------------------------------------------------------------ 假客户端

class FakeMessage:
    def __init__(self, content):
        self.content = content


class FakeCompletion:
    def __init__(self, content):
        self.choices = [SimpleNamespace(message=FakeMessage(content))]
        self.usage = SimpleNamespace(prompt_tokens=120, completion_tokens=30)


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0) if self.replies else '{"answer": ["A"], "confidence": 0.1}'
        if isinstance(reply, Exception):
            raise reply
        return FakeCompletion(reply)


def make_solver(replies, **overrides):
    config = AIConfig(api_key="sk-test", **overrides)
    completions = FakeCompletions(replies)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    solver = AISolver(config, client=client)
    solver._cache = None  # 单元测试默认不走缓存, 需要时单独开
    return solver, completions


def q(qid="1", title="1+1=?", qtype="single", options="A.1\nB.2\nC.3"):
    return {"id": qid, "title": title, "type": qtype, "options": options, "answerField": {}}


# ------------------------------------------------------------------ 用例

def test_basic_single():
    solver, calls = make_solver(['{"answer": ["B"], "confidence": 0.93, "reason": "算术"}'],
                                thinking=False)
    answer = solver.solve(q())
    assert answer and answer.answer == ["B"], answer
    assert abs(answer.confidence - 0.93) < 1e-6
    # 请求里必须有 JSON 模式; 显式关思考时要真的告诉服务端
    kwargs = calls.calls[0]
    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert kwargs["model"] == "deepseek-flash"
    print("[ok] 单选题: 直接拿字母 B, 请求带 JSON 模式")


def test_option_content_to_letter():
    """模型如果回了选项内容, 应该反查出字母, 而不是白白丢掉"""
    solver, _ = make_solver(['{"answer": ["2", "4"], "confidence": 0.8}'])
    answer = solver.solve(q(qtype="multiple", options="A.1\nB.2\nC.3\nD.4"))
    assert answer and answer.answer == ["B", "D"], answer
    print("[ok] 多选: 选项内容 2/4 反查成字母 B/D")


def test_continuous_letters():
    solver, _ = make_solver(['{"answer": ["BD"], "confidence": 0.8}'])
    answer = solver.solve(q(qtype="multiple", options="A.1\nB.2\nC.3\nD.4"))
    assert answer and answer.answer == ["B", "D"], answer
    print("[ok] 多选: 连续字母 BD 被拆成 B/D")


def test_repair_on_invalid_letter():
    """给了不存在的选项 Z, 应带着可选范围重问一次"""
    solver, calls = make_solver([
        '{"answer": ["Z"], "confidence": 0.5}',
        '{"answer": ["C"], "confidence": 0.7}',
    ])
    answer = solver.solve(q())
    assert answer and answer.answer == ["C"], answer
    assert answer.repaired is True
    assert len(calls.calls) == 2, "应该问了两次"
    repair_messages = calls.calls[1]["messages"]
    assert any("可选字母" in m["content"] or "有效选项" in m["content"] for m in repair_messages)
    print("[ok] 非法答案触发纠错重问, 第二次拿到 C")


def test_markdown_wrapper():
    solver, _ = make_solver(['```json\n{"answer": ["A"], "confidence": 0.6}\n```'])
    answer = solver.solve(q())
    assert answer and answer.answer == ["A"]
    print("[ok] 兼容 ```json 代码块包裹")


def test_plain_letter_fallback():
    solver, _ = make_solver(["B"])
    answer = solver.solve(q())
    assert answer and answer.answer == ["B"], answer
    print("[ok] 模型只回一个字母 B 也能解析")


def test_cache_hit():
    config = AIConfig(api_key="sk-test", cache=True, cache_file="ai_cache_test.json")
    completions = FakeCompletions(['{"answer": ["B"], "confidence": 0.9}'])
    solver = AISolver(config, client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))
    try:
        first = solver.solve(q())
        second = solver.solve(q())
        assert first and second and second.from_cache is True
        assert len(completions.calls) == 1, "同样的题目不应该重复请求"
        assert solver.cache_hits == 1
        print("[ok] 相同题目命中缓存, 只请求 1 次")
    finally:
        if os.path.isfile("ai_cache_test.json"):
            os.remove("ai_cache_test.json")


def test_empty_content_retry():
    solver, calls = make_solver(["", '{"answer": ["A"], "confidence": 0.8}'], max_retries=3)
    answer = solver.solve(q())
    assert answer and answer.answer == ["A"]
    assert len(calls.calls) == 2
    print("[ok] 返回空内容时自动重试一次")


def test_auth_error_no_retry():
    error = Exception("Unauthorized")
    error.status_code = 401
    solver, calls = make_solver([error, '{"answer": ["A"]}'], max_retries=3)
    answer = solver.solve(q())
    assert answer is None
    assert len(calls.calls) == 1, "401 不应该重试"
    print("[ok] API Key 无效时直接放弃, 不做无意义重试")


def test_engine_with_ai_only():
    """题库停用、只有 AI 时, 引擎应把来源标成 ai 并算进覆盖率"""
    solver, _ = make_solver([
        '{"answer": ["B"], "confidence": 0.9}',
        '{"answer": ["正确"], "confidence": 0.9}',
        '{"answer": ["水", "H2O"], "confidence": 0.8}',
    ])
    tiku = SimpleNamespace(DISABLE=True)
    engine = AnswerEngine(tiku, ai_solver=solver, ai_mode="fallback")
    questions = [
        q("1", "1+1=?", "single", "A.1\nB.2\nC.3"),
        q("2", "地球是圆的", "judgement", "A.对\nB.错"),
        q("3", "水由什么组成", "completion", ""),
    ]
    report = engine.solve_all(questions, title="AI 自测")
    sources = [item.source for item in report.solved]
    assert sources == ["ai", "ai", "ai"], sources
    assert questions[0]["answerField"]["answer1"] == "B"
    assert questions[1]["answerField"]["answer2"] == "true", "判断应转成 true/false"
    assert questions[2]["answerField"]["answer3"] == "水\nH2O", "多空用换行分隔"
    assert report.ai_count == 3 and abs(report.cover_rate - 1.0) < 1e-6
    print("[ok] 引擎联动:", report.summary())


def test_engine_fallback_order():
    """fallback 模式下题库优先, 题库没命中才问 AI"""
    solver, calls = make_solver(['{"answer": ["A"], "confidence": 0.9}'])
    tiku = SimpleNamespace(DISABLE=False, query=lambda info: "3", judgement_select=lambda a: True)
    engine = AnswerEngine(tiku, ai_solver=solver, ai_mode="fallback")
    question = q("1", "1+1=?", "single", "A.1\nB.2\nC.3")
    solved = engine.solve_one(question)
    assert solved.source == "tiku" and solved.answer == "C", solved
    assert len(calls.calls) == 0, "题库命中时不应该请求 AI"

    # 题库查不到 -> AI 兜底
    tiku_miss = SimpleNamespace(DISABLE=False, query=lambda info: None, judgement_select=lambda a: True)
    engine2 = AnswerEngine(tiku_miss, ai_solver=solver, ai_mode="fallback")
    solved2 = engine2.solve_one(q("2", "不会的题", "single", "A.1\nB.2\nC.3"))
    assert solved2.source == "ai" and solved2.answer == "A", solved2
    print("[ok] fallback 顺序: 题库优先, 未命中才用 AI")


def test_config():
    assert normalize_ai_config(None).enabled is False
    assert normalize_ai_config({"api_key": "sk-1", "mode": "priority"}).enabled is True
    assert normalize_ai_config({"api_key": "sk-1", "mode": "xxx"}).mode == "fallback"
    assert normalize_ai_config({"api_key": "", "mode": "fallback"}).mode == "off", "没填 key 应自动关闭"
    config = normalize_ai_config({"api_key": "sk-1234567890abcd", "concurrency": "99", "temperature": "5"})
    assert config.concurrency == 16 and config.temperature == 2.0
    assert config.masked_key().startswith("sk-123") and "***" in config.masked_key()
    # 深度思考默认开启(用户要求), 且 max_tokens 自动抬高, 否则思考会吃光配额
    default_cfg = normalize_ai_config({"api_key": "sk-1"})
    assert default_cfg.thinking is True and default_cfg.max_tokens == 4096, default_cfg
    # 显式关掉仍然是关的
    assert normalize_ai_config({"api_key": "sk-1", "thinking": "false"}).thinking is False
    thinking = normalize_ai_config({"api_key": "sk-1", "thinking": "true", "max_tokens": "1024"})
    assert thinking.thinking is True and thinking.max_tokens == 4096, thinking.max_tokens
    # 用户自己填了更大的值就不动
    big = normalize_ai_config({"api_key": "sk-1", "thinking": "true", "max_tokens": "8192"})
    assert big.max_tokens == 8192
    print("[ok] 配置归一化: 缺 key 自动关闭, 非法值回退, 开思考自动抬 max_tokens")


def test_thinking_request():
    """开思考时不注入 disabled, 且 thinking 直接留在 extra_body 之外"""
    solver, calls = make_solver(['{"answer": ["B"], "confidence": 0.9}'], thinking=True)
    answer = solver.solve(q())
    assert answer and answer.answer == ["B"]
    kwargs = calls.calls[0]
    assert "extra_body" not in kwargs, "开思考时不应该再发 disabled"
    assert kwargs["max_tokens"] >= 4096, "开思考后输出配额应被抬高"
    print("[ok] 深度思考: 不注入 disabled, max_tokens 抬到 4096")


def test_thinking_json_400_fallback():
    """思考模式 + JSON 输出被端点拒绝(400)时, 去掉 response_format 再问一次"""
    error = Exception("response_format is not supported with thinking")
    error.status_code = 400
    solver, calls = make_solver(
        [error, '{"analysis": "先推理", "answer": ["B"], "confidence": 0.9}'],
        thinking=True, max_retries=3,
    )
    answer = solver.solve(q())
    assert answer and answer.answer == ["B"], answer
    assert len(calls.calls) == 2
    assert "response_format" not in calls.calls[1], "第二次应改为纯文本输出"
    print("[ok] 思考模式不支持 JSON 时自动降级为文本解析")


def test_image_question_marked():
    """题干里有 <img> 时, 标记 has_image 并在提示里说明模型看不到图"""
    solver, calls = make_solver(['{"analysis": "看图", "answer": ["A"], "confidence": 0.6}'])
    answer = solver.solve(q(title='根据下图回答问题 <img src="https://x/1.png">'))
    assert answer and answer.has_image is True
    user_msg = calls.calls[0]["messages"][1]["content"]
    assert "图片" in user_msg and "confidence" in user_msg
    assert solver.image_questions == 1
    print("[ok] 图片题被标记, 提示词里说明看不到图")


def test_engine_marks_review():
    solver, _ = make_solver(['{"answer": ["A"], "confidence": 0.8}'])
    engine = AnswerEngine(SimpleNamespace(DISABLE=True), ai_solver=solver, ai_mode="fallback")
    question = q("9", '下图是什么 <img src="https://x/2.png">', "single", "A.1\nB.2")
    report = engine.solve_all([question], title="图片题")
    assert report.review_count == 1
    assert report.solved[0].needs_review is True
    assert "人工复核" in report.summary()
    print("[ok] 引擎汇总:", report.summary())


def test_cache_namespace_and_confidence():
    """缓存 key 跟模型/版本走; 低置信度的猜测不落缓存"""
    path = "ai_cache_test.json"
    for leftover in (path, path + ".tmp"):
        if os.path.isfile(leftover):
            os.remove(leftover)
    try:
        item = q()
        assert AICache(path, "deepseek-flash").make_key(item) != AICache(path, "deepseek-reasoner").make_key(item), \
            "换模型必须换缓存 key"

        config = AIConfig(api_key="sk-test", cache=True, cache_file=path)
        completions = FakeCompletions(['{"answer": ["B"], "confidence": 0.2}'] * 3)
        solver = AISolver(config, client=SimpleNamespace(chat=SimpleNamespace(completions=completions)))
        solver.solve(item)
        assert len(solver._cache) == 0, "低置信度答案不该写缓存"
        solver.solve(item)
        assert len(completions.calls) == 2, "没缓存就应该重新请求"
        print("[ok] 缓存: key 带模型/版本, 低置信度不落盘")
    finally:
        for leftover in (path, path + ".tmp"):
            if os.path.isfile(leftover):
                os.remove(leftover)


def main():
    test_basic_single()
    test_option_content_to_letter()
    test_continuous_letters()
    test_repair_on_invalid_letter()
    test_markdown_wrapper()
    test_plain_letter_fallback()
    test_cache_hit()
    test_empty_content_retry()
    test_auth_error_no_retry()
    test_engine_with_ai_only()
    test_engine_fallback_order()
    test_config()
    test_thinking_request()
    test_thinking_json_400_fallback()
    test_image_question_marked()
    test_engine_marks_review()
    test_cache_namespace_and_confidence()
    print("\nAI 答题离线自测全部通过")


if __name__ == "__main__":
    main()
