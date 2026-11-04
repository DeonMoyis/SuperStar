<p align="center">
  <a href="https://www.springing.top" target="blank">
    <img src="images/logo.png" alt="Logo" width="156" height="156">
  </a>
  <h2 align="center" style="font-weight: 600">Spring-Superstar</h2>
  <p align="center">
    学习通 / 超星 / 泛雅 · 全自动刷课脚本<br>
    视频 · 文档 · 章节测验 · 作业 · 考试 一条龙，内置 <b>DeepSeek AI 搜题</b><br>
    为 <b>GitHub Actions</b> 设计：Fork 一次，手机点一下就能跑
  </p>
</p>

> 如果这个项目帮到了你，点个 Star 就是最大的支持。

---

## 目录

- [这是什么](#这是什么)
- [为什么主推 GitHub Actions](#为什么主推-github-actions)
- [五分钟上手（Actions）](#五分钟上手actions)
- [安全：公开仓库能不能用](#安全公开仓库能不能用)
- [Actions 参数速查](#actions-参数速查)
- [程序到底在做什么](#程序到底在做什么)
- [本地运行 / Docker](#本地运行--docker)
- [配置文件详解](#配置文件详解)
- [命令行参数](#命令行参数)
- [常见问题](#常见问题)
- [项目结构](#项目结构)
- [深入原理](#深入原理)

---

## 这是什么

一个跑在你自己账号上的刷课脚本。它会登录学习通，把课程里的**视频、文档、章节测验、作业、考试**按顺序做完，并在需要的时候调用题库或大模型来答题。

三件和其他刷课脚本不一样的事：

| | |
| --- | --- |
| **不怕时长校验** | 视频锁死 1 倍速，且同一时刻只播一个，真实耗时和真看一遍一致，不会因为「学得太快」被判定无效 |
| **能分段跑** | 可以设置单次运行时长，到点自动收尾退出，下次接着来，符合「不能一次刷完」的要求 |
| **跑在云上** | 主推 GitHub Actions，不用开电脑、不用挂机，手机也能操作 |

---

## 为什么主推 GitHub Actions

这不是「顺便支持一下」，而是这个项目最合适的用法：

| | Actions | 本地挂机 |
| --- | --- | --- |
| 电脑要开着吗 | 不要 | 要 |
| 断网/关机影响 | 无 | 直接中断 |
| 手机能不能操作 | 能 | 不能 |
| 跑 40 小时的课 | 分多次自动接力 | 得挂 40 小时 |
| 费用（公开仓库） | 免费 | 电费 |

因为视频是**按真实时间播放**的（1 倍速、串行），一门 40 小时的课就是 40 小时。你不可能一直开着电脑等它，所以「云端分段跑」几乎是唯一实用的方式：

```
每次跑 90 分钟 → 自动退出 → 定时空闲时再来 → 直到刷完
```

---

## 五分钟上手（Actions）

### 第 1 步：Fork 本仓库

点右上角 `Fork`，创建到你自己的账号下。

> Fork 出来的仓库默认是**公开**的，这是正常的、也是推荐的 —— 原因见 [安全](#安全公开仓库能不能用)。

### 第 2 步：填 Secrets

进入你 fork 后的仓库：

```
Settings → Secrets and variables → Actions → New repository secret
```

逐个添加：

| 名称 | 必填 | 内容 |
| --- | --- | --- |
| `CHAOXING_USERNAME` | ✅ | 学习通手机号 |
| `CHAOXING_PASSWORD` | ✅ | 学习通密码 |
| `CHAOXING_COURSE_LIST` | 选填 | 课程 ID，逗号分隔。留空 = 学全部课程 |
| `DEEPSEEK_API_KEY` | 选填 | DeepSeek 的 Key。**填了才会开启 AI 搜题** |

填完是这个样子：

```
CHAOXING_USERNAME   ●●●●●
CHAOXING_PASSWORD   ●●●●●
DEEPSEEK_API_KEY    ●●●●●
```

> 课程 ID 怎么找？在网页版打开某门课程，地址栏里 `courseId=` 后面那串数字就是。

### 第 3 步：跑一次

```
Actions → 左侧「刷课」→ 右侧 Run workflow → Run workflow
```

等几秒刷新页面，会出现一条运行记录，点进去看实时日志。

**第一次建议先短跑一次**，确认能登录、能识别课程：

```
Run workflow → max_runtime 填 10 → Run
```

十分钟后回来看日志末尾，如果出现 `本次运行 ... 分钟 / 上限 10 分钟` 就说明一切正常。

### 第 4 步：按需开启定时

默认**不会**自动跑，只有你手动点或者 push 代码才触发。想要「自动接力」，编辑仓库里的 `.github/workflows/main.yml`，取消这两行的注释：

```yaml
schedule:
  - cron: "0 2,6,10,14 * * *"
```

⚠️ **GitHub 的 cron 用的是 UTC 时间，不是北京时间**，要减 8 小时：

| 你想要的北京时间 | 应该写的 cron |
| --- | --- |
| 每天 08:00 | `0 0 * * *` |
| 每天 10 点、22 点 | `0 2,14 * * *` |
| 每 4 小时 | `0 0,4,8,12,16,20 * * *` |

上面的例子 `0 2,6,10,14 * * *` = 北京时间 10 点、14 点、18 点、22 点，一天 4 次。

改完 commit，就会按新时间自动跑。想临时停掉就去 Actions 页面点 `Disable workflow`。

---

## 安全：公开仓库能不能用

**能用，而且公开仓库才是对的。** 这里必须纠正一个流传很广的说法 ——「Fork 之后一定要把仓库设为私有，否则密码泄露被盗号」。

这个说法把风险点搞错了。

### 真正的风险在哪

风险不是「仓库公开」，而是「**把密码写进了仓库里的文件**」。

```
❌ 危险：把 config.ini 提交上去
   config.ini 里的 username= / password= 是明文，一旦 commit，
   公开仓库所有人可见，私有仓库的所有协作者和历史记录也可见。

✅ 正确：密码只存在 Secrets 里
   GitHub Secrets 是加密存储的，提交进仓库的任何文件里都没有它，
   在 Actions 日志里也会被自动打码成 ***。
```

所以：

- **仓库公开 → 完全没问题**，因为里面根本没有密码；
- **仓库私有 → 也不会更安全**，如果你把 `config.ini` 提交了，私有仓库同样有一堆人能看（协作者、fork 者、以及哪天不小心转公开）。

本项目已经处理好了这件事：`.gitignore` 里排除了 `config*.ini`、`cookies.txt`、`ai_cache.json`，正常操作**不可能**把密码提交上去。

### 那为什么以前会有人被盗号

两种情况：

1. 用了别人的「整合版」「一键包」，里面塞了会外传凭据的代码；
2. 自己手动改了 workflow，把密码写进了 yml 或者 echo 到日志里。

**所以最该检查的是 workflow 和代码，而不是仓库的可见性。**

### 三条硬规则

1. **密码只填在 `Settings → Secrets and variables → Actions`**，任何文件里都不要出现；
2. **Fork 别人的魔改版之前，先看 `main.yml` 和代码** —— workflow 是能读到你的 Secrets 的，一段 `curl` 就能把它发到别人的服务器。不确定就用官方仓库；
3. **别用来源不明的「一键包 / 整合包 / exe」**。

### Secrets 和 Variables 的区别

Actions 页面上有两个地方，别搞混：

| | Secrets | Variables |
| --- | --- | --- |
| 加密 | ✅ 加密，写入后不可查看 | ❌ 明文，可随时查看 |
| 日志里 | 自动打码 | 原样显示 |
| 用在哪 | 账号、密码、API Key | 开关、时长、策略 |
| 本项目的键 | `CHAOXING_USERNAME`<br>`CHAOXING_PASSWORD`<br>`CHAOXING_COURSE_LIST`<br>`DEEPSEEK_API_KEY` | `MAX_RUNTIME_MINUTES`<br>`ANSWER_SUBMIT`<br>`AUTO_ANSWER`<br>`ANSWER_EXAM` |

一句话：**秘密放 Secrets，开关放 Variables。**

---

## Actions 参数速查

除了上面四个 Secret，还有四个 Variable 可以调（`Settings → Secrets and variables → Actions → Variables 标签页`）：

| Variable | 取值 | 默认 | 作用 |
| --- | --- | --- | --- |
| `MAX_RUNTIME_MINUTES` | 数字 | `110` | 单次最多跑多少分钟，到点收尾退出 |
| `ANSWER_SUBMIT` | `auto` / `always` / `never` | `auto` | 答题后提交策略，见 [作业与考试](#作业与考试) |
| `AUTO_ANSWER` | `true` / `false` | 有 Key 就 `true` | 是否做作业/考试 |
| `ANSWER_EXAM` | `true` / `false` | `true` | **是否让程序代考**，设 `false` 只做作业 |
| `ANSWER_ORDER` | `after` / `before` / `both` | `after` | 作业和刷课谁先做，见 [作业与考试](#作业与考试) |
| `VIDEO_DEBUG` | `true` / `false` | `false` | 排查「视频刷不过去」时设 `true`，诊断数据存到 `video_debug/` |

`ANSWER_EXAM = false` 是为了这种情况：考试通常是「学完一定进度才解锁」的，而且**次数有限、交了就改不了**。你可能希望作业交给程序，考试自己考。设成 `false` 后，程序会跳过考试列表，作业照做。

> 手动触发时还有一个 `max_runtime` 输入框，只影响这一次，优先级高于 `MAX_RUNTIME_MINUTES`。

### 时长和免费额度

- **公开仓库**：Actions 标准 runner **不计量、不收费**，跑多久都行。
- **私有仓库**：每月 2000 分钟免费额度。

这也是为什么公开仓库更适合本项目 —— 它本来就要跑很多小时。如果因为「担心密码」而设成私有，反而既没更安全，又把免费额度烧光了。

### 一次运行里发生了什么

```
1. 检出代码 → 装 Python 3.13 → pip install
2. 从 Secrets 读出账号密码，作为环境变量传给程序
3. 程序登录学习通，拉课程列表
4. 刷这门课的视频/文档，每小节先看完内容，再做本节测验（视频 1 倍速串行）
5. 再处理这门课的作业 + 考试（除非设了 ANSWER_EXAM=false）
6. 这门课收工，进入下一门课
7. 到达时长上限 → 收尾退出 → job 结束
```

**顺序是按课程闭环的**：第 1 门课的「学内容 → 做本节测验 → 做作业/考试」全部走完，才进第 2 门课。中途撞上时长上限也没关系，前面的课是完整的，下次从断点接着来。想改成先做题再刷课用 `ANSWER_ORDER=before`，想前后各扫一遍用 `both`。

下一次运行时，程序会重新拉课程列表，**已完成的自动跳过**，从没做完的地方接着来。所以你可以放心让它一天跑多次。

---

## 程序到底在做什么

### 一次运行的完整流程

```
登录
 └─ 拉取课程列表（可只学指定课程）
     └─ 逐门课处理：
         ├─ 拉章节列表
         │   └─ 逐章处理任务点（文档 → 视频 → 测验 → 阅读 → 直播）
         │       · 视频：1 倍速真实播放，走完全程才上报完成
         │       · 章节测验：题库/AI 搜题 → 覆盖率达标才提交
         │
         └─ 拉作业列表 + 考试列表
             ├─ 逐条判断状态（未交/未开始才处理，已完成/已批阅跳过）
             ├─ 考试默认排在作业前面
             ├─ 打开发题页，自动点「开始考试」/「继续作答」
             ├─ 解析题目（含超星字体加密解密）
             ├─ 逐题作答：题库 → AI → 随机兜底
             └─ 覆盖率达标 → 提交；不达标 → 只存草稿
```

### 播放节奏：为什么必须 1 倍速

学习通上报的进度是「**视频内时间**」，而服务端校验时比对的是「**真实经过的时间**」。两倍速下，真实 30 分钟走完 60 分钟的视频，这个比值一眼就不对，后果通常是：任务点显示完成但后台没有时长、章节被回滚、甚至整门课成绩不出。

比倍速更隐蔽的是**并发**：如果同时有 4 个视频在「播放」，每个进度都正常，但真实耗时被压成了总时长的 1/4 —— 和倍速效果一样。

所以现在是：

| 项目 | 行为 |
| --- | --- |
| 播放倍速 | 锁死 1.0，配置里写 2 也只是打条警告然后忽略 |
| 视频推进 | 全局一个「播放位」，同一时刻只有一个视频在走 |
| 章节并发 | 默认 1，按顺序处理 |
| 章节内任务点 | 按页面顺序依次处理，**先学完内容再做本节测验** |
| 章节测验 | 全局一个「作答位」，一次只提交一份（避免触发风控） |
| 文档 / 阅读 | 串行，跟着同一节的顺序走 |
| 作业 / 考试 | 单线程串行，默认排在每门课刷课**之后**（`ANSWER_ORDER`） |

**代价很直白：课程有多少小时视频，就要跑多少小时。**这是刻意设计，不是 bug，也是 [`MAX_RUNTIME_MINUTES`](#actions-参数速查) 存在的理由。

日志会告诉你实际跑了多久：

```
任务完成: 第一章 视频 | 视频时长 12:30 | 起始进度 0:00 | 本次实际耗时 12:30
课程 高等数学 处理结束 | 累计播放 96.5 分钟, 排队等待 0.0 分钟
```

「起始进度」是服务器上已记录的进度，所以**断点续播时实际耗时小于总长是正常的**。

### 分段刷课

设置 `MAX_RUNTIME_MINUTES = 90` 后：

- 到点后**不再开始**新章节、新作业、新考试；
- 正在播的视频、正在写的作业**照常做完**，不打断；
- 全部收尾后正常退出，返回码 0；
- 下次运行从断点继续。

所以实际结束时间 ≈ `上限 + 最后一个任务的剩余时长`，这是**软上限**。想卡得更紧，就把上限设得比单集视频长度大一些。

### 作业与考试

程序扫的是课程里的「作业」和「考试」两个列表，另外**章节里的测验任务点**（很多课是「每小节一个视频 + 一份测验」）也走同一套答题逻辑：

| 要做的题 | 从哪来 | 谁来答 |
| --- | --- | --- |
| 章节测验 | 章节任务点（`workid`） | 题库 → 大模型 → 随机兜底 |
| 作业 | 课程「作业」列表 | 题库 → 大模型 → 随机兜底 |
| 考试 | 课程「考试」列表 | 题库 → 大模型 → 随机兜底 |

只要填了 `DEEPSEEK_API_KEY`，**没配题库也能答章节测验**（以前必须配 `[tiku]`，否则静默跳过，日志里只有一句「未找到tiku配置」）。

**作业和刷课谁先做**（`ANSWER_ORDER`）：

| 值 | 行为 | 适合 |
| --- | --- | --- |
| `after` | 先刷课，刷完这门课再做作业/考试（**默认**） | 绝大多数情况：作业和考试都要等学习进度解锁 |
| `before` | 先做作业/考试，再刷视频 | 作业是独立布置、不随进度解锁的课 |
| `both` | 刷课前扫一遍，刷完再扫一遍 | 不确定作业是不是随进度解锁 |

另外，**章节内的小测验（每小节那份作业）永远排在视频之后**：程序会先把这一节的视频/文档学完，再去做这一节的测验，和你在网页上的操作顺序一致。

**作业**：一般可以补交，交给程序通常没坏处。

**考试**：情况不一样 —— 通常要学完一定进度才解锁，次数有限，交卷后不能改。所以是否代考由你决定：

```bash
--no-exam      # 只做作业，考试自己考
--exam         # 考试也让程序做
（都不写）      # 跟随配置文件 [answer] exam
```

在 Actions 里对应 `ANSWER_EXAM = false`。

另外两个和安全相关的机制：

- **考试优先**：考试排在作业前面，避免时间盒到点时把考试全跳过；
- **开考保护**：距本次时长上限不足 15 分钟就不开考。因为一点「开始考试」倒计时就走，中途被掐断等于白费一次机会 —— 不如留给下次。

提交策略（`ANSWER_SUBMIT`）：

| 值 | 行为 | 适合 |
| --- | --- | --- |
| `auto` | 覆盖率 ≥ 90% 才提交，否则只存草稿 | 默认，推荐 |
| `always` | 答完直接交 | 确认过答案质量后 |
| `never` | 只填不交，你自己检查后手动交 | **第一次用强烈建议** |

### AI 搜题

填了 `DEEPSEEK_API_KEY` 就会启用。流程是：

```
题库搜题（可配多个题库，依次回退）
  ├─ 命中 → 用题库答案
  └─ 没命中 → DeepSeek
        ├─ 强制 JSON 输出，选择题只回选项字母
        ├─ 答案不合法（比如回了不存在的选项）→ 带上「可选范围」再问一次
        ├─ 同一道题只问一次，答案缓存到 ai_cache.json
        └─ 还是失败 → 随机答案兜底
```

- 默认 `mode = fallback`：题库优先、AI 兜底，最省钱；
- 费用参考：一份 30 题的作业约几分钱，缓存命中后不再产生费用；
- 只回答问题本身，不做任何浏览器自动化。

---

## 本地运行 / Docker

Actions 是推荐方式，但本地也能跑。

```bash
git clone <你的仓库地址>
cd SuperStar
pip install -r requirements.txt

cp config_template.ini config.ini   # config.ini 已被 .gitignore 排除，不会被提交
# 编辑 config.ini，填上 [common] 的 username / password

python main.py -c config.ini
```

常用组合：

```bash
# 刷课 + 顺手做作业和考试
python main.py -c config.ini --auto-answer

# 期末冲刺：只做题，不刷视频
python main.py -c config.ini --answer-only

# 只做作业，考试自己考
python main.py -c config.ini --auto-answer --no-exam

# 先只填不交，人工检查答案
python main.py -c config.ini --auto-answer --answer-submit never

# 这次最多跑 90 分钟
python main.py -c config.ini --max-runtime 90
```

Docker：

```bash
docker build -t superstar .
docker run -d --name superstar \
  -v /你的路径/config.ini:/config/config.ini \
  superstar --auto-answer
```

---

## 配置文件详解

配置文件是 `config_template.ini`，共 5 节。**只想要 AI 答题的话，改 `[common]` 和 `[ai]` 两节就够。**

### `[common]`

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `use_cookies` | 用 `cookies.txt` 登录，忽略账号密码 | `false` |
| `username` / `password` | 学习通手机号 / 密码 | — |
| `course_list` | 课程 ID，逗号分隔，留空则询问 | — |
| `speed` | **已固定 1 倍速**，写别的会被改回 | `1` |
| `jobs` | 同时处理的章节数（视频仍串行） | `1` |
| `max_runtime_minutes` | 单次运行时长上限，`0` = 不限 | `0` |
| `notopen_action` | 章节未开放时 `retry` 重试 / `continue` 跳过 | `retry` |

### `[tiku]` 题库

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `provider` | 题库列表，逗号分隔按顺序回退 | `TikuYanxi` |
| `submit` | 旧开关，**已由 `[answer] submit_policy` 接管** | `false` |
| `cover_rate` | 最低覆盖率 | `0.9` |
| `tokens` | 言溪 / LIKE 题库的 token | 空 |
| `true_list` / `false_list` | 判断题的「对/错」写法 | 见模板 |

可用：`TikuYanxi`、`TikuLike`、`TikuGo`、`TikuAdapter`、`AI`、`SiliconFlow`、`DeepSeek`。

`DeepSeek` 复用 `[ai]` 的 Key 和缓存，所以想让题库优先、AI 兜底：

```ini
[tiku]
provider = TikuGo,DeepSeek
```

> 不配 `[tiku]` 也能答题：章节测验会自动降级到大模型。这一节只在你有题库、
> 想让它先于大模型命中（省钱）时才需要填。

### `[ai]` DeepSeek

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `api_key` | Key，留空则 AI 关闭 | 空 |
| `base_url` | OpenAI 兼容地址 | `https://api.deepseek.com` |
| `model` | 模型名 | `deepseek-flash` |
| `mode` | `fallback` 题库优先 / `priority` AI 优先 / `off` 关闭 | `fallback` |
| `temperature` | 答题建议 0~0.3 | `0.2` |
| `max_tokens` | 单题输出上限（开 `thinking` 时自动抬到 4096） | `1024` |
| `timeout` | 单次请求超时（秒） | `60` |
| `max_retries` | 单题重试次数 | `3` |
| `concurrency` | 并发请求数，太高会 429 | `4` |
| `min_interval_seconds` | 两次请求最小间隔，0 不限 | `0` |
| `thinking` | 深度思考：推理题更准，但更慢更贵 | `false` |
| `cache` / `cache_file` | 缓存开关 / 路径 | `true` / `ai_cache.json` |
| `check_connection` | 启动时校验 Key | `true` |

任何 OpenAI 兼容模型都能用：改 `base_url` + `model` 即可，代码不用动。

### `[answer]` 作业 / 考试

| 键 | 说明 | 默认 |
| --- | --- | --- |
| `enable` | 总开关，等价于 `--auto-answer` | `false` |
| `work` | 是否处理作业 | `true` |
| `exam` | **是否处理考试**，等价于 `--exam` / `--no-exam` | `true` |
| `order` | 作业和刷课谁先做：`before` / `after` / `both` | `before` |
| `submit_policy` | `auto` / `always` / `never`（作业、考试、章节测验共用） | `auto` |
| `min_cover_rate` | `auto` 模式的最低覆盖率 | `0.9` |
| `max_tasks` | 每门课最多处理多少份 | `20` |
| `exam_priority` | 考试排在作业前面 | `true` |
| `exam_min_remaining` | 剩余不足这么多分钟就不开考，`0` 关闭 | `15` |
| `debug` | 保存原始页面到 `answer_debug/` | `false` |

### `[notification]`

支持 `ServerChan`、`Qmsg`、`Bark`、`Telegram`，结束时推送结果。不需要就留空。

---

## 命令行参数

```bash
python main.py --help
```

| 参数 | 说明 |
| --- | --- |
| `-c, --config` | 指定配置文件 |
| `-u` / `-p` | 手机号 / 密码 |
| `-l, --list` | 课程 ID，逗号分隔 |
| `-s, --speed` | 已废弃，倍速固定 1 |
| `-j, --jobs` | 并行章节数，默认 1 |
| `--max-runtime` | 单次运行时长上限（分钟） |
| `-v, --verbose` | 输出 DEBUG 日志 |
| `--auto-answer` | 刷完课顺便做作业和考试 |
| `--answer-only` | 只做题，不刷视频 |
| `--answer-submit` | `auto` / `always` / `never` |
| `--answer-order` | `after` / `before` / `both`：作业和刷课谁先做 |
| `--exam` / `--no-exam` | **是否处理考试** |
| `--answer-debug` | 保存原始页面，排查解析问题 |
| `--video-debug` | 保存卡住视频的卡片/状态/响应，排查「刷不过去」 |
| `--deepseek-key` | DeepSeek Key |
| `--ai-model` | 模型名，默认 `deepseek-flash` |
| `--ai-mode` | `fallback` / `priority` / `off` |
| `--ai-thinking` / `--no-ai-thinking` | 开启 / 关闭深度思考（默认关） |

命令行优先级高于配置文件。

---

## 常见问题

**Q：Fork 之后要不要把仓库设为私有？**
不用。密码存在 Secrets 里，仓库里没有敏感信息，公开是安全的。而且公开仓库的 Actions 不计量 —— 这个项目动辄要跑几十小时，设成私有反而会很快耗尽免费额度。详见 [安全](#安全公开仓库能不能用)。

**Q：Actions 跑起来没反应 / 报 `Invalid username or password`？**
先检查 Secrets 名字是否完全一致（区分大小写）：`CHAOXING_USERNAME`、`CHAOXING_PASSWORD`。手机号不要带 `+86` 或空格。

**Q：日志报 `API Key 无效 (401)`？**
Key 填错或复制多了空格，去 DeepSeek 控制台重新生成。

**Q：报 `账户余额不足 (402)`？**
DeepSeek 需要充值，新账号赠送额度用完后返回 402。

**Q：报 `请求过于频繁 (429)`？**
把 `[ai]` 的 `concurrency` 调到 2，或 `min_interval_seconds` 设为 1。

**Q：作业/考试没解析出题目？**
用 `--answer-debug` 跑一次，原始页面会存到 `answer_debug/`。看是列表页还是答题页结构变了（列表页一条都没解析出来时，即使没开 debug 也会自动存）。

**Q：某个视频进度到 100% 了，日志却报「任务点卡死」或「进度已到片尾但仍未通过」？**

说明这个任务点靠上报进度已经推不动了，服务端不认。加 `--video-debug` 重跑一次，日志和 `video_debug/` 会直接告诉你是哪一种：

| 日志里的判定 | 含义 | 能不能自动解决 |
| --- | --- | --- |
| `卡片里带弹窗题` | 视频中途有内嵌小题，没答服务端就不算完成 | 程序会识别并用大模型作答，答案打在 `[视频弹窗题]` 日志里 |
| `该视频开启人脸采集` | `videoFaceCaptureEnc` 非空，要真人过人脸 | ❌ 纯 HTTP 做不到，得去网页端 / App |
| `卡片里没发现弹窗题…` | 既没题目也没人脸，通常是服务端状态延迟或风控 | 换个时间重跑，或把 `--jobs` 保持 1 |

被跳过的任务点不会让整章失败，程序会在结束时汇总成「需人工处理的视频任务点」，下次运行会自动重新检查。

**Q：考试答完了但没交卷？**
部分考试的提交入口是 JS 动态触发的。程序会优先用页面的 `<form action>`，失败再试固定接口，仍有小概率对不上。这种情况答案通常已经填好，去网页端手动点交卷即可，日志会出现 `提交返回异常, 请到网页端手动确认`。

**Q：不想让程序代考怎么办？**
加 `--no-exam`，或者 Actions 里设 `ANSWER_EXAM = false`。程序会完全跳过考试列表。

**Q：程序跑着跑着自己退出了，是崩了吗？**
看最后一行。如果是 `本次运行 90.2 分钟 / 上限 90 分钟 (已到上限)`，那是到点正常收尾。

**Q：设了 90 分钟，为什么跑了 110 分钟才停？**
上限只在任务之间检查，不打断正在播的视频。一个 40 分钟的视频从第 80 分钟开始播，就得等它播完。

**Q：日志说「暂不开始考试，留给下次运行」？**
剩余时间不到 `exam_min_remaining`（默认 15 分钟）。考试开考即倒计时，被掐断会浪费次数，所以主动不开考。下次运行会重新扫描到它，不会漏。

**Q：下次运行会不会漏掉上次没做完的章节？**
不会。每次都重新拉列表，按服务端记录的完成状态判断。

**Q：会不会重复提交已经交过的作业？**
不会。状态为「已完成 / 已提交 / 已批阅 / 已过期」的条目会被跳过。

---

## 项目结构

```
SuperStar/
├── main.py                  # 入口：参数解析、任务调度、刷课 + 答题
├── config_template.ini      # 配置模板
├── .github/workflows/main.yml  # Actions 工作流（主推用法）
├── api/
│   ├── base.py              # 超星核心 API：登录、章节、视频、文档、章节测验
│   ├── answer.py            # 题库实现（言溪/LIKE/GO/Adapter/AI/DeepSeek）
│   ├── answer_engine.py     # 通用答题引擎：答案匹配、覆盖率、表单拼装
│   ├── ai_solver.py         # DeepSeek 搜题：提示词、纠错、重试、缓存、并发
│   ├── auto_answer.py       # 作业/考试：列表扫描、抓题、作答、提交
│   ├── decode.py            # 页面解析（题目、章节、任务点）
│   ├── playback.py          # 播放/作答节奏：1 倍速 + 播放位/作答位
│   ├── runtime_budget.py    # 单次运行时长上限（时间盒）
│   ├── font_decoder.py      # 超星字体加密解密
│   ├── notification.py      # 外部通知
│   └── live.py / live_process.py
├── tests/                   # 离线自测，不需要账号和 Key
└── docs/技术文档.md          # 深入原理与二次开发
```

### 本地自测

四个测试全部**离线**，用假的 HTTP 响应跑完整链路，不需要登录也不需要 API Key：

```bash
pip install -r requirements.txt
python tests/test_auto_answer.py     # 列表解析 / 作答匹配 / 提交策略 / 考试开关
python tests/test_ai_solver.py       # JSON 解析 / 字母纠错 / 缓存 / 重试
python tests/test_playback.py        # 倍速强制 / 播放位与作答位互斥（约 15 秒）
python tests/test_runtime_budget.py  # 时长上限 / 到点跳过 / 不卡死（约 10 秒）
```

改完解析逻辑或提示词后先跑一遍，确认没把已有功能改坏。后两个会真的等时间，慢是正常的。

---

## 深入原理

如果想搞清楚：

- 程序是怎么和超星通信的（登录加密、进度上报、字体反混淆）；
- 播放闸门、时间盒这些机制在代码里怎么落地；
- 想自己加一个题库、改提示词、接别的模型；
- 或者只是想知道每个配置项背后到底发生了什么；

请看 **[docs/技术文档.md](docs/技术文档.md)** —— 从架构到每个模块的实现细节，以及二次开发的完整指引。

---

## 鸣谢

<a href="https://github.com/Samueli924/chaoxing" target="blank">灵感来源：Samueli924/chaoxing</a>

+ ZhangBowen
+ LiJiahui
+ YinZhengke
+ LiuLizhe
+ JiangQin

> 本项目仅供学习交流使用。请遵守所在学校的规定，因使用本程序产生的一切后果由使用者自行承担。
