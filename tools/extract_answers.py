# -*- coding: utf-8 -*-
"""从运行日志里抽出「做过的题 + 当时给的答案」，生成人工校订答案的骨架

用法
----
    python tools/extract_answers.py                      # 读 chaoxing.log
    python tools/extract_answers.py run.log other.log    # 读指定日志(Actions 日志下载下来也能喂)
    python tools/extract_answers.py --source ai,tiku     # 连题库命中的也一起列出来
    python tools/extract_answers.py --out my_answers.json

日志里每个「填写答案为 X (来源: ai)」都是一条记录。脚本把它们去重后写进
`manual_answers.json`（已存在的条目不会动，只补新的），每条带一个 note 提示你要核对。

接着你只需要打开 `manual_answers.json`，把答错的那几条 `answer` 改成正确答案，
删掉 `note`（或者留着也不影响），提交推送到仓库 —— 之后不管是你还是别人跑，
命中这道题就直接用你写的答案，不会再问 AI。

为什么不能直接改 `ai_cache.json`？
  * 它在 .gitignore 里，Actions 又用 actions/cache 把它恢复回来，提交上去会被覆盖；
  * key 里带缓存版本号和模型名，提示词一改整批失效；
  * 只认「一模一样的题干+选项」，多一个空格就 miss，换个人跑等于没有。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# 让脚本能直接跑（python tools/extract_answers.py）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from api.manual_answers import (  # noqa: E402
    DEFAULT_ANSWERS_FILE,
    normalize_title,
)

# 日志行: "... 1【判断题】xxx（） 填写答案为 true (来源: ai)"
_LINE = re.compile(r"填写答案为\s*(?P<answer>.*?)\s*\(来源:\s*(?P<source>[a-zA-Z_]+)\)\s*$")

# 题干里的题型标记 -> 引擎的题型名
_TYPE_HINTS = [
    ("判断题", "judgement"),
    ("单选题", "single"),
    ("多选题", "multiple"),
    ("不定项选择题", "multiple"),
    ("填空题", "completion"),
    ("简答题", "shortanswer"),
    ("论述题", "shortanswer"),
]


def guess_type(title: str) -> str:
    for word, name in _TYPE_HINTS:
        if word in title:
            return name
    return ""


def human_answer(q_type: str, answer: str) -> str:
    """把提交格式的答案换成人类写法，方便直接对照"""
    text = (answer or "").strip()
    if q_type == "judgement":
        if text.lower() in {"true", "正确"}:
            return "正确"
        if text.lower() in {"false", "错误"}:
            return "错误"
    return text


def collect(paths: list[str], sources: set[str]) -> list[dict]:
    found: dict[str, dict] = {}
    total_lines = 0
    for path in paths:
        if not os.path.isfile(path):
            print(f"[跳过] 找不到日志: {path}")
            continue
        with open(path, "r", encoding="utf-8", errors="ignore") as fp:
            for line in fp:
                if "填写答案为" not in line:
                    continue
                total_lines += 1
                match = _LINE.search(line.rstrip("\n"))
                if not match:
                    continue
                source = match.group("source")
                if sources and source not in sources:
                    continue
                answer = match.group("answer").strip()
                if not answer or answer in {"<空>", "None"}:
                    continue
                head = line[: match.start()]
                # loguru 的格式是 "时间 | 级别 | 模块:函数:行 - 消息"
                cut = head.rfind(" - ")
                title = (head[cut + 3:] if cut >= 0 else head).strip()
                if not title:
                    continue
                key = normalize_title(title)
                if not key or key in found:
                    continue
                q_type = guess_type(title)
                found[key] = {
                    "title": title,
                    "type": q_type,
                    "answer": human_answer(q_type, answer),
                    "_source": source,
                }
    print(f"扫描 {len(paths)} 个日志, 共看到 {total_lines} 条作答记录, 去重后 {len(found)} 道题")
    return list(found.values())


def load_existing(path: str) -> dict:
    if not os.path.isfile(path):
        return {"version": 1, "entries": []}
    try:
        with open(path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        return {"version": 1, "entries": []}
    if not isinstance(data, dict):
        return {"version": 1, "entries": []}
    data.setdefault("version", 1)
    if not isinstance(data.get("entries"), list):
        data["entries"] = []
    return data


def merge(data: dict, items: list[dict]) -> tuple[int, int]:
    existing = {normalize_title(e.get("title")) for e in data["entries"] if isinstance(e, dict)}
    added = skipped = 0
    for item in items:
        key = normalize_title(item["title"])
        if key in existing:
            skipped += 1
            continue
        data["entries"].append({
            "title": item["title"],
            "type": item["type"],
            "answer": item["answer"],
            "note": f"待核对: 原来是 {item['_source']} 答的, 答错就改成正确答案",
        })
        existing.add(key)
        added += 1
    return added, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="从运行日志生成人工校订答案骨架")
    parser.add_argument("logs", nargs="*", default=None, help="日志文件(默认 chaoxing.log)")
    parser.add_argument("--out", default=DEFAULT_ANSWERS_FILE, help=f"输出文件(默认 {DEFAULT_ANSWERS_FILE})")
    parser.add_argument(
        "--source", default="ai,random",
        help="只导出这些来源的题(逗号分隔, 默认 ai,random; 想全都要就填 all)",
    )
    args = parser.parse_args()

    logs = args.logs or ["chaoxing.log"]
    sources = set()
    if args.source and args.source.lower() != "all":
        sources = {s.strip() for s in args.source.split(",") if s.strip()}

    items = collect(logs, sources)
    if not items:
        print("没抽到题。要么日志里还没有「填写答案为 …」的记录, 要么来源被过滤掉了。")
        return 1

    data = load_existing(args.out)
    added, skipped = merge(data, items)
    with open(args.out, "w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

    print(f"已写入 {args.out}: 新增 {added} 条, 已存在跳过 {skipped} 条, 现在共 {len(data['entries'])} 条")
    print("下一步: 打开这个文件, 把答错的 answer 改成正确答案, 然后 commit + push。")
    print("提示: 判断题写 正确 / 错误; 选择题写 A 或 ABD; 填空题多个空用 | 分隔。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
