<p align="center">
  <a href="https://www.springing.top" target="blank">
    <img src="images/logo.png" alt="Logo" width="156" height="156">
  </a>
  <h2 align="center" style="font-weight: 600">Spring-Superstar</h2>
  <p align="center">
    学习通在线刷课脚本 超星 学习通 云端刷课
  </p>
</p>

本项目宗旨是帮助大学生们解放双手，根据教程操作后就可以完成刷课，简单易懂，仅需手机就可以使用

>如果本项目对希望各位同学们给本仓库点一个免费的Star或者给小春子点一个Follow 谢谢大家！

![截图](/images/star.png)

## 用前必读必读！
默认fork仓库之后是公开的状态，也就是说你的账号和密码是处于一个明文状态！！很危险！！建议把仓库权限改为私有

之前本仓库就因为明文账号密码泄露导致一些同学账号被盗拿来恶作剧(不得不说世界上还是坏人多)，故提前在此说明，希望大家周知

## 快速开始

<a href="https://blog.springing.top/p/20241119/" target="blank">点击我查看操作说明</a>

<a href="https://github.com/Samueli924/chaoxing" target="blank">灵感来源</a>

## 自动答题（作业 / 考试）

原来的脚本只会处理「章节任务点」里的测验，课程里「作业」和「考试」列表是没人管的。
新增的自动答题模块把这一块补上了，流程是：

```
课程 -> 扫作业列表 / 考试列表 -> 挑出未完成的条目 -> 打开答题页抓题
     -> 题库/AI 搜题 -> 自动填答 -> 覆盖率达标才提交，不达标只保存草稿
```

### 三种用法

```bash
# 1. 刷完课顺便把作业和考试做了（最常用）
python main.py -c config.ini --auto-answer

# 2. 只做作业和考试，不刷视频（期末冲刺用）
python main.py -c config.ini --answer-only

# 3. 答完先不提交，自己检查一遍再点提交
python main.py -c config.ini --auto-answer --answer-submit never
```

`--answer-submit` 三个可选值：

| 取值 | 行为 |
| --- | --- |
| `auto`（默认） | 题库覆盖率达到 `min_cover_rate` 才提交，否则只保存草稿 |
| `always` | 答完直接提交 |
| `never` | 只填写并保存，留给你自己手动提交 |

### 配置文件写法

在 `config.ini` 里加上这一段（模板见 `config_template.ini`）：

```ini
[answer]
enable = true          ; 总开关，等价于 --auto-answer
work = true            ; 是否做作业
exam = true            ; 是否做考试
submit_policy = auto   ; auto / always / never
min_cover_rate = 0.9   ; auto 模式的最低覆盖率
max_tasks = 20         ; 每门课最多处理多少份，防止跑飞
debug = false          ; 解析不出题目时改成 true，会把原始页面存到 answer_debug/
```

命令行参数的优先级高于配置文件，所以在配置文件里关掉的情况下也可以用
`--auto-answer` 临时打开。

### 出问题了怎么看

超星的作业/考试页面改版比较频繁。如果日志里出现
`没能从页面解析出题目`，用 `--answer-debug` 再跑一次：

```bash
python main.py -c config.ini --auto-answer --answer-debug
```

抓到的原始页面会存到 `answer_debug/`，看一眼就知道是列表页结构变了还是答题页结构变了。
列表页如果一条都没解析出来，即使没开 `debug` 也会自动存一份，方便定位。

### 本地自测（不需要登录）

```bash
pip install -r requirements.txt
python tests/test_auto_answer.py
```

这个测试用假的 HTTP 响应跑通「列表解析 -> 抓题 -> 作答 -> 提交」整条链路，
改完解析逻辑可以先跑它，确认没把已有功能改坏。

> 提醒：作业和考试的次数、时限通常比章节测验严格，用之前先确认 `submit_policy`，
> 建议先用 `never` 跑一遍看看答案质量，再改成 `auto`。

+ZhangBowen
+LiJiahui
+YinZhengke
+LiuLizhe
+JiangQin
