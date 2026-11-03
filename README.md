<p align="center">
  <a href="https://www.springing.top" target="blank">
    <img src="images/logo.png" alt="Logo" width="156" height="156">
  </a>
  <h2 align="center" style="font-weight: 600">Spring-Superstar</h2>
  <p align="center">
    学习通在线刷课脚本 · 超星 / 学习通 / 泛雅<br>
    视频 · 文档 · 章节测验 · <b>作业</b> · <b>考试</b> 全自动，内置 <b>DeepSeek AI 搜题答题</b>
  </p>
</p>

本项目宗旨是帮助大学生们解放双手，根据教程操作后就可以完成刷课，简单易懂，仅需手机就可以使用。

> 如果本项目对你有帮助，希望给本仓库点一个免费的 Star，或者给小春子点一个 Follow，谢谢大家！

![截图](/images/star.png)

---

## 目录

- [功能特性](#功能特性)
- [用前必读](#用前必读)
- [快速开始](#快速开始)
- [配置详解](#配置详解)
- [命令行参数](#命令行参数)
- [自动答题是怎么工作的](#自动答题是怎么工作的)
- [项目结构](#项目结构)
- [本地测试](#本地测试)
- [常见问题](#常见问题)
- [鸣谢](#鸣谢)

---

## 功能特性

| 任务类型 | 支持 | 说明 |
| --- | --- | --- |
| 视频 / 音频 | ✅ | 支持倍速（最大 2x），多章节并行 |
| 文档 / 阅读 | ✅ | 自动完成阅读任务点 |
| 章节测验 | ✅ | 题库搜题 + AI 搜题，按覆盖率决定是否提交 |
| **作业** | ✅ | 扫描作业列表 → 自动作答 → 按策略提交 |
| **考试** | ✅ | 扫描考试列表 → 进入考试 → 自动作答 → 按策略交卷 |
| **DeepSeek AI 搜题** | ✅ | 填一个 API Key 即可，答案自动纠错 + 本地缓存 |
| 直播任务 | ✅ | 挂机处理 |
| 外部通知 | ✅ | Server 酱 / Qmsg / Bark / Telegram |

**AI 搜题是这一版的重点**：题库搜不到时，程序会把题目和选项交给 DeepSeek，要求它直接返回选项字母、判断题结论或填空内容；返回结果不合法会自动带上「可选范围」再问一次；同一道题只问一次，答案缓存到 `ai_cache.json`。

---

## 用前必读

1. **仓库一定要设为私有**。fork 之后默认是公开仓库，配置文件里的账号密码是明文，曾有同学因此被盗号。`Settings → General → Danger Zone → Change visibility → Private`。
2. **作业和考试通常有次数 / 时限限制**，比章节测验严格得多。第一次跑建议用 `--answer-submit never`（只填写不提交），人工检查一遍答案质量后再改成 `auto`。
3. 本项目仅供学习交流使用，请遵守所在学校的规定，因使用本程序产生的一切后果由使用者自行承担。

---

## 快速开始

### 方式一：本地运行

```bash
git clone <你的仓库地址>
cd SuperStar
pip install -r requirements.txt

# 复制配置模板并填写账号
cp config_template.ini config.ini

python main.py -c config.ini --auto-answer --deepseek-key sk-你的key
```

只想做题、不刷视频（期末冲刺）：

```bash
python main.py -c config.ini --answer-only --deepseek-key sk-你的key
```

### 方式二：GitHub Actions（手机也能用）

在仓库 `Settings → Secrets and variables → Actions` 里添加：

| 名称 | 类型 | 是否必填 | 说明 |
| --- | --- | --- | --- |
| `CHAOXING_USERNAME` | Secret | 必填 | 手机号 |
| `CHAOXING_PASSWORD` | Secret | 必填 | 密码 |
| `CHAOXING_COURSE_LIST` | Secret | 选填 | 课程 ID，逗号分隔，留空则学全部 |
| `DEEPSEEK_API_KEY` | Secret | 选填 | **填了就会自动开启 AI 搜题 + 作业/考试作答** |
| `ANSWER_SUBMIT` | Variable | 选填 | `auto` / `always` / `never`，默认 `auto` |

配置好后到 `Actions` 页面点 `Run workflow` 即可，也支持定时任务（取消 `main.yml` 里 `schedule` 的注释）。

### 方式三：Docker

```bash
docker build -t superstar .
docker run -d --name superstar \
  -v /你的路径/config.ini:/config/config.ini \
  superstar
```

---

## 配置详解

配置文件基于 `config_template.ini`，共 5 个配置节。**只想用 AI 答题的话，只改 `[common]` 和 `[ai]` 两节就够了。**

### `[common]` 基础设置

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `use_cookies` | 用 `cookies.txt` 登录，忽略账号密码 | `false` |
| `username` / `password` | 学习通手机号 / 密码 | — |
| `course_list` | 要学习的课程 ID，逗号分隔，留空则询问 | — |
| `speed` | 视频倍速，1 ~ 2 | `1` |
| `jobs` | 同时处理的章节数 | `4` |
| `notopen_action` | 章节未开放时：`retry` 重试 / `continue` 跳过 | `retry` |

### `[ai]` DeepSeek AI 搜题答题

**三步就能用：**

1. 到 [platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys) 申请一个 API Key；
2. 填到配置文件 `[ai]` 的 `api_key`（或者命令行 `--deepseek-key`）；
3. 运行时带上 `--auto-answer` 或 `--answer-only`。

```ini
[ai]
api_key = sk-你的key
model = deepseek-flash
mode = fallback
```

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `api_key` | DeepSeek API Key，留空则 AI 功能关闭 | 空 |
| `base_url` | OpenAI 兼容接口地址 | `https://api.deepseek.com` |
| `model` | 模型名，`deepseek-flash` 即 DeepSeek-V4.1-Flash | `deepseek-flash` |
| `mode` | `fallback` 题库优先 / `priority` AI 优先 / `off` 关闭 | `fallback` |
| `temperature` | 温度，答题建议 0 ~ 0.3 | `0.2` |
| `max_tokens` | 单题回答长度上限 | `1024` |
| `timeout` | 单次请求超时（秒） | `60` |
| `max_retries` | 单题失败重试次数 | `3` |
| `concurrency` | 并发请求数，太高会报 429 | `4` |
| `min_interval_seconds` | 两次请求的最小间隔（秒），0 不限 | `0` |
| `thinking` | 是否开启深度思考，答题建议关闭 | `false` |
| `cache` / `cache_file` | 答案缓存开关 / 路径 | `true` / `ai_cache.json` |
| `check_connection` | 启动时检查 Key 是否可用 | `true` |

**`mode` 怎么选：**

- `fallback`（默认）：先查题库，题库没收录的题再用 AI。最省钱，推荐。
- `priority`：全部交给 AI，AI 失败才回落题库。适合完全没配题库的人。
- `off`：只用题库和随机答案。

**关于费用**：AI 是逐题请求的，一份 30 题的作业大约消耗 3~5 万 token。参考 DeepSeek 官方定价（`deepseek-flash`，低峰时段输入约 \$0.15 / 百万 token、输出约 \$0.6 / 百万 token），**一份作业的成本通常在几分钱**。所有答案都会缓存，下次遇到同样的题直接命中缓存、不再产生费用。具体价格以 [官方页面](https://api-docs.deepseek.com/quick_start/pricing) 为准。

> 想换成别的 OpenAI 兼容模型（比如通义、Kimi、硅基流动）也可以：把 `base_url` 和 `model` 改成对应的值即可，代码不用动。

### `[tiku]` 题库设置

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `provider` | 题库列表，逗号分隔按顺序回退 | `TikuYanxi` |
| `submit` | 章节测验是否自动提交 | `false` |
| `cover_rate` | 最低题库覆盖率 | `0.9` |
| `tokens` | 言溪 / LIKE 题库的 token | 空 |
| `true_list` / `false_list` | 判断题的「对/错」表述 | 见模板 |

可用 `provider`：`TikuYanxi`、`TikuLike`、`TikuGo`、`TikuAdapter`、`AI`、`SiliconFlow`、`DeepSeek`。

其中 `DeepSeek` 会复用 `[ai]` 节里填的 Key、缓存和限流器，所以想让章节测验也用 AI，写成：

```ini
[tiku]
provider = TikuGo,DeepSeek
```

### `[answer]` 作业 / 考试自动答题

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `enable` | 总开关，等价于 `--auto-answer` | `false` |
| `work` / `exam` | 是否处理作业 / 考试 | `true` / `true` |
| `submit_policy` | `auto` 按覆盖率 / `always` 总是提交 / `never` 只保存 | `auto` |
| `min_cover_rate` | `auto` 模式的最低覆盖率（题库 + AI 合计） | `0.9` |
| `max_tasks` | 每门课最多处理多少份 | `20` |
| `debug` | 保存原始页面到 `answer_debug/` | `false` |

### `[notification]` 外部通知

支持 `ServerChan`、`Qmsg`、`Bark`、`Telegram`，任务结束后推送结果。不需要可以留空。

---

## 命令行参数

```bash
python main.py --help
```

| 参数 | 说明 |
| --- | --- |
| `-c, --config` | 指定配置文件 |
| `-u, -p` | 手机号 / 密码（不用配置文件时） |
| `-l, --list` | 要学习的课程 ID，逗号分隔 |
| `-s, --speed` | 视频倍速（最大 2） |
| `-j, --jobs` | 并行章节数 |
| `-v, --verbose` | 输出 DEBUG 日志 |
| `--auto-answer` | 刷完课顺便做作业和考试 |
| `--answer-only` | 只做作业和考试，不刷视频 |
| `--answer-submit` | `auto` / `always` / `never` |
| `--answer-debug` | 保存作业/考试原始页面，排查解析问题 |
| `--deepseek-key` | DeepSeek API Key，填了即启用 AI |
| `--ai-model` | 指定模型名，默认 `deepseek-flash` |
| `--ai-mode` | `fallback` / `priority` / `off` |

---

## 自动答题是怎么工作的

```
课程
 ├─ 作业列表 / 考试列表
 │    └─ 逐条判断状态（未交 / 未开始 才处理，已完成 / 已批阅 跳过）
 │
 ├─ 打开答题页（考试会先自动点「开始考试」）
 │    └─ 解析题目：题干、选项、题型（自动解密超星字体加密）
 │
 ├─ 逐题作答
 │    ① 题库搜题（可配多个题库回退）
 │    ② 题库没命中 → DeepSeek AI
 │         · 强约束 JSON 输出，选择题只回选项字母
 │         · 答案不合法 → 带上「可选范围」再问一次（自动纠错）
 │         · 命中缓存则跳过请求
 │    ③ 都没有 → 随机答案兜底
 │
 └─ 覆盖率 = (题库命中 + AI 作答) / 总题数
      ├─ 达标 → 自动提交
      └─ 不达标 → 只保存草稿，日志提示你手动检查
```

几个刻意的设计：

- **提交地址取自答题页自己的 `<form action>`**，而不是写死接口。作业和考试的提交地址不同，页面给什么就用什么，命中率高得多。
- **列表解析不写死 CSS 选择器**，而是从页面所有链接和 `onclick` 里按关键字提取答题页地址，并原样保留页面给的 query 参数（`enc`、`answerId`、`cpi` 这些不用猜）。超星改版时不容易直接失效。
- **AI 只回答题目本身**，不做任何浏览器自动化，也不会替你点交卷以外的操作。

---

## 项目结构

```
SuperStar/
├── main.py                 # 入口：配置解析、任务调度、刷课 + 答题
├── config_template.ini     # 配置模板（含 [ai] DeepSeek 配置）
├── api/
│   ├── base.py             # 超星核心 API：登录、章节、视频、文档、章节测验
│   ├── answer.py           # 题库实现（言溪/LIKE/GO/Adapter/AI/SiliconFlow/DeepSeek）
│   ├── answer_engine.py    # 通用答题引擎：答案匹配、覆盖率、表单拼装
│   ├── ai_solver.py        # DeepSeek 搜题答题：提示词、纠错、重试、缓存、并发
│   ├── auto_answer.py      # 作业/考试：列表扫描、抓题、作答、提交
│   ├── decode.py           # 页面解析（题目、章节、任务点）
│   ├── font_decoder.py     # 超星字体加密解密
│   ├── notification.py     # 外部通知
│   └── live.py / live_process.py  # 直播任务
├── tests/
│   ├── test_auto_answer.py # 作业/考试离线自测
│   └── test_ai_solver.py   # AI 答题离线自测
└── resource/font_map_table.json
```

---

## 本地测试

两个测试都是**离线**的，用假的 HTTP 响应和假的 AI 客户端跑通完整链路，不需要登录也不需要 API Key：

```bash
pip install -r requirements.txt

python tests/test_auto_answer.py   # 列表解析 / 作答匹配 / 提交策略 / 端到端
python tests/test_ai_solver.py     # JSON 解析 / 选项字母纠错 / 缓存 / 重试 / 引擎联动
```

改完解析逻辑或提示词后，先跑这两个确认没把已有功能改坏。

---

## 常见问题

**Q：日志报 `API Key 无效 (401)`？**
Key 填错了，或者复制时多了空格。到 DeepSeek 控制台重新生成一个。

**Q：报 `账户余额不足 (402)`？**
DeepSeek 账户需要充值，新账号赠送额度用完后会返回 402。

**Q：报 `请求过于频繁 (429)`？**
把 `[ai]` 的 `concurrency` 调小（比如 2），或把 `min_interval_seconds` 设为 1。

**Q：作业/考试没解析出题目？**
用 `--answer-debug` 再跑一次，原始页面会存到 `answer_debug/`。看一眼是列表页还是答题页结构变了；列表页如果一条都没解析出来，即使没开 debug 也会自动存一份。

**Q：考试答完了但没交卷？**
考试页面的提交入口有些是 JS 动态触发的，程序会优先用页面表单地址、失败再试固定接口，但仍有小概率对不上。这种情况下答案通常已经填好，去网页端手动点交卷即可。日志里会出现 `提交返回异常, 请到网页端手动确认`。

**Q：AI 速度有点慢？**
默认对 4 道题并发。可以调大 `concurrency`，但别超过 8，容易撞限流。也可以把 `mode` 设成 `fallback`，让题库先扛掉大部分题。

**Q：能换别的模型吗？**
可以。任何 OpenAI 兼容接口都行：改 `base_url` 和 `model` 两个字段即可，例如硅基流动、通义、Kimi。

**Q：会不会重复提交已经交过的作业？**
不会。列表页状态为「已完成 / 已提交 / 已批阅 / 已过期」的条目会被跳过，只处理未交、未开始的。

---

## 鸣谢

<a href="https://github.com/Samueli924/chaoxing" target="blank">灵感来源：Samueli924/chaoxing</a>

+ ZhangBowen
+ LiJiahui
+ YinZhengke
+ LiuLizhe
+ JiangQin
