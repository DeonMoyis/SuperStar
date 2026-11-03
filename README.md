<p align="center">
  <a href="https://www.springing.top" target="blank">
    <img src="images/logo.png" alt="Logo" width="156" height="156">
  </a>
  <h2 align="center" style="font-weight: 600">Spring-Superstar</h2>
  <p align="center">
    学习通在线刷课脚本 · 超星 / 学习通 / 泛雅<br>
    视频 · 文档 · 章节测验 · <b>作业</b> · <b>考试</b> 全自动，内置 <b>DeepSeek AI 搜题答题</b><br>
    视频统一 <b>1 倍速 + 单视频串行</b>，真实耗时对齐课程时长
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
- [播放节奏](#播放节奏)
- [分段刷课](#分段刷课)
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
| 视频 / 音频 | ✅ | **强制 1 倍速、单视频串行**，真实耗时对齐课程时长 |
| 文档 / 阅读 | ✅ | 自动完成阅读任务点 |
| 章节测验 | ✅ | 题库搜题 + AI 搜题，按覆盖率决定是否提交 |
| **作业** | ✅ | 扫描作业列表 → 自动作答 → 按策略提交 |
| **考试** | ✅ | 扫描考试列表 → 进入考试 → 自动作答 → 按策略交卷 |
| **DeepSeek AI 搜题** | ✅ | 填一个 API Key 即可，答案自动纠错 + 本地缓存 |
| **分段刷课** | ✅ | 可设单次运行时长，到点收尾退出，配合定时任务自动接力 |
| 直播任务 | ✅ | 挂机处理 |
| 外部通知 | ✅ | Server 酱 / Qmsg / Bark / Telegram |

**AI 搜题是这一版的重点**：题库搜不到时，程序会把题目和选项交给 DeepSeek，要求它直接返回选项字母、判断题结论或填空内容；返回结果不合法会自动带上「可选范围」再问一次；同一道题只问一次，答案缓存到 `ai_cache.json`。

---

## 用前必读

1. **仓库一定要设为私有**。fork 之后默认是公开仓库，配置文件里的账号密码是明文，曾有同学因此被盗号。`Settings → General → Danger Zone → Change visibility → Private`。
2. **刷视频请预留真实时间**。本项目的倍速被锁死在 1 倍速，且同一时刻只播一个视频，40 小时的课程就要跑 40 小时左右。这不是可调参数，原因见 [播放节奏](#播放节奏)。如果学校要求不能一次刷完，用 [`max_runtime_minutes`](#分段刷课) 分段跑。
3. **作业和考试通常有次数 / 时限限制**，比章节测验严格得多。第一次跑建议用 `--answer-submit never`（只填写不提交），人工检查一遍答案质量后再改成 `auto`。
4. 本项目仅供学习交流使用，请遵守所在学校的规定，因使用本程序产生的一切后果由使用者自行承担。

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
| `MAX_RUNTIME_MINUTES` | Variable | 选填 | 单次运行时长上限（分钟），默认 `110`。见 [分段刷课](#分段刷课) |

配置好后到 `Actions` 页面点 `Run workflow` 即可。手动触发时还能临时填一个 `max_runtime`，只影响这一次。

也支持定时任务：把 `main.yml` 里 `schedule` 的注释取消，就能按 cron 自动跑，配合 `MAX_RUNTIME_MINUTES` 就是**分段刷课**。

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
| `speed` | **已固定为 1 倍速**，写其他值会被强制改回 | `1` |
| `jobs` | 同时处理的章节数，视频始终串行播放 | `1` |
| `max_runtime_minutes` | 单次运行时长上限（分钟），`0` = 不限制 | `0` |
| `notopen_action` | 章节未开放时：`retry` 重试 / `continue` 跳过 | `retry` |

### 播放节奏

**这一版把倍速彻底锁死在 1 倍速，并且同一时刻只允许一个视频推进进度。**

#### 为什么不再支持倍速

上报给学习通的进度是「视频内的时间」，而服务端校验时会拿它和「真实经过的时间」对照。2 倍速下，真实 30 分钟走完 60 分钟的视频，这个比值一眼就不对，常见后果：

- 任务点不计入，页面显示「已完成」但后台没有时长；
- 章节被回滚，需要重刷；
- 极端情况下整门课的学习记录都不正常。

所以 `speed` 这个配置项虽然还留着，但**写任何非 1 的值都会被强制改回 1**，程序启动时会打印一条提示。命令行 `--speed 2` 也一样被忽略。

#### 为什么之前 1 倍速也觉得耗时偏短

这不是倍速参数的问题，而是**并发**。旧版默认 `jobs = 4`，章节之间并行，章节内还有 5 个任务点并行，等于 4~9 个视频在同时「播放」。服务端只看到每个视频的进度都在涨，但你的真实耗时被压缩成了总时长的 1/N —— 效果和倍速完全一样，而且更隐蔽。

现在改成了**全局播放闸门**：

| 项目 | 旧行为 | 现在 |
| --- | --- | --- |
| 视频推进 | 多个视频同时涨进度 | 排队，一个播完再下一个 |
| 章节并发 | 默认 4 | 默认 1（按顺序处理） |
| 文档 / 测验 / 阅读 | 并发 | **仍然并发**，不受影响 |
| 总耗时 | 约 视频总时长 ÷ 并发数 | 约 视频时长之和 |

一句话：**这门课有多少小时的视频，就要跑多少小时。**这是刻意设计，不是 bug。

#### 怎么确认它真的在按 1 倍速跑

每个视频播完会打印一行：

```
任务完成: 第一章 视频 | 视频时长 12:30 | 起始进度 0:00 | 本次实际耗时 12:30
```

每门课结束再打印累计播放时长：

```
课程 高等数学 处理结束 | 累计播放 96.5 分钟, 排队等待 0.0 分钟
```

「起始进度」是服务器上已经记录的进度，所以**断点续播时实际耗时小于视频总长是正常的**，只补没看的部分。但如果「本次实际耗时」明显短于「视频时长 − 起始进度」，程序会额外给一条 WARNING，那才是服务端提前放行的信号。

#### 代价

刷课时间会变得和真看一遍差不多。这门课有 40 小时视频，就要跑 40 小时左右。如果你只是想快速标记任务点完成、学校也不查时长，这个策略并不适合你；但只要卡时长，这就是唯一稳妥的做法。

### 分段刷课

有些学校明确要求**不能一次性把整门课刷完**，需要分几天、每天学几段。这个版本加了「单次运行时长上限」：

- 到点后**不再开始**新的章节 / 作业 / 考试；
- 正在播的视频、正在写的作业**照常跑完**，不会被打断；
- 全部收尾后程序自己退出，返回码 0；
- 下次运行从断点继续（服务端记着已完成的任务点，不会重做）。

#### 三种设置方式

```bash
# 1. 命令行：这次最多跑 90 分钟
python main.py -c config.ini --max-runtime 90
```

```ini
# 2. 配置文件 [common]
max_runtime_minutes = 90   ; 0 或不填 = 不限制
```

```yaml
# 3. GitHub Actions：在 Variables 里加 MAX_RUNTIME_MINUTES = 90
#    workflow 已经接好，不需要改代码
```

优先级：命令行 > 配置文件；Actions 里是「手动触发时填的值 > Variables > 默认 110」。

#### 和定时任务配合

把 `.github/workflows/main.yml` 里 `schedule` 的注释取消，就变成「自动分段刷课」：

```yaml
schedule:
  - cron: "0 0,4,8,12,16,20 * * *"   # 每 4 小时跑一次
```

配合 `MAX_RUNTIME_MINUTES = 90`，效果就是：每 4 小时跑 90 分钟，跑完自动退出，4 小时后自己再来。

**注意 Actions 免费额度**：私有仓库每月 2000 分钟。每次 90 分钟 × 每天 6 次 = 540 分钟/天，一个月远超额度。想长期白嫖就把时长压到 30~40 分钟，或者把频率降到每天 1~2 次：

| 频率 | 单次时长 | 每天消耗 | 大约能用 |
| --- | --- | --- | --- |
| 每天 1 次 | 60 分钟 | 60 分钟 | 33 天 |
| 每天 2 次 | 45 分钟 | 90 分钟 | 22 天 |
| 每 4 小时 | 90 分钟 | 540 分钟 | 3~4 天 |

public 仓库的 Actions 不计费，可以放心跑。

#### 结束时会告诉你

```
已达到单次运行时长上限 90 分钟 (实际已运行 90.2 分钟): 不再开始新任务, 正在执行的任务跑完后退出
[时长上限] 本课程还有 4 个章节没开始, 已跳过, 下次运行会继续处理
本次运行 91.5 分钟 / 上限 90 分钟 (已到上限)
```

#### 一个前提：这是「软上限」

上限只在**任务之间**检查，不会打断正在播放的视频。所以一个 90 分钟的视频从第 80 分钟开始播，程序会等到第 170 分钟才退出。这是刻意的 —— 中途掐断视频反而容易留下半截进度、触发回滚。

实际结束时间大约等于 `上限 + 单个任务剩余时长`。想卡得更紧，就把上限设得比「单集视频长度」大一些。

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
| `-s, --speed` | 已废弃，倍速固定 1，传了也不生效 |
| `-j, --jobs` | 并行章节数（默认 1，视频仍串行） |
| `--max-runtime` | 单次运行时长上限（分钟），到点收尾退出 |
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
│   ├── playback.py         # 播放节奏：1 倍速固定 + 单视频串行闸门
│   ├── runtime_budget.py   # 单次运行时长上限（时间盒）
│   ├── font_decoder.py     # 超星字体加密解密
│   ├── notification.py     # 外部通知
│   └── live.py / live_process.py  # 直播任务
├── tests/
│   ├── test_auto_answer.py # 作业/考试离线自测
│   ├── test_playback.py    # 1 倍速 / 视频串行自测
│   ├── test_runtime_budget.py  # 单次运行时长 / 优雅停止自测
│   └── test_ai_solver.py   # AI 答题离线自测
└── resource/font_map_table.json
```

---

## 本地测试

四个测试都是**离线**的，用假的 HTTP 响应和假的 AI 客户端跑通完整链路，不需要登录也不需要 API Key：

```bash
pip install -r requirements.txt

python tests/test_auto_answer.py   # 列表解析 / 作答匹配 / 提交策略 / 端到端
python tests/test_ai_solver.py     # JSON 解析 / 选项字母纠错 / 缓存 / 重试 / 引擎联动
python tests/test_playback.py      # 倍速强制 1x / 播放闸门互斥 / 真实耗时对齐（约 15 秒）
python tests/test_runtime_budget.py # 时长上限 / 到点跳过 / 调度器不卡死（约 10 秒）
```

改完解析逻辑或提示词后，先跑这几个确认没把已有功能改坏。`test_playback.py` 和 `test_runtime_budget.py` 会真的等时间，跑得慢是正常的。

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

**Q：程序跑着跑着自己退出了，是崩了吗？**
先看最后一行日志。如果是 `本次运行 90.2 分钟 / 上限 90 分钟 (已到上限)`，那是到点正常收尾，不是崩溃。把 `max_runtime_minutes` 调大或者设成 `0` 就不会中途退出。

**Q：设了 90 分钟，为什么实际跑了 110 分钟才停？**
上限只在任务之间检查，不会打断正在播的视频。一个 40 分钟的视频从第 80 分钟开始播，就得等它播完。想卡得紧一些，把上限设成比单集视频长度大一点。

**Q：下次运行会不会把上次没做的章节漏掉？**
不会。程序每次都重新拉取章节列表，按服务端记录的任务点完成状态判断，已完成的跳过、没做的继续做。

---

## 鸣谢

<a href="https://github.com/Samueli924/chaoxing" target="blank">灵感来源：Samueli924/chaoxing</a>

+ ZhangBowen
+ LiJiahui
+ YinZhengke
+ LiuLizhe
+ JiangQin
