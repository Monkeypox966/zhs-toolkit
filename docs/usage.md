# 使用说明

## 安装与登录

在项目目录运行 `uv sync --locked`，使用 `uv run zhs --check` 检查连接。
默认二维码登录，终端显示方式可用 `--show-in-terminal` 或 `--no-show-in-terminal` 指定。
已有 Cookie 保存在 `var/cookies.json`，无需每次重新扫码。

配置文件为 `config/config.json`。初次运行会自动生成，也可以从同目录的示例文件复制后修改。
账号密码登录取决于平台验证要求，可用 `--no-qrlogin` 选择现有的账号密码登录流程。

## 参数

| 参数 | 用途 |
| --- | --- |
| `--check` | 只检查登录与课程列表 |
| `-f`, `--fetch` | 保存全部类型课程到 `var/execution.json` |
| `-c`, `--course ID [ID ...]` | 指定课程 |
| `-v`, `--videos ID [ID ...]` | 指定视频，需同时指定课程 |
| `-ai`, `--aicourse COURSE_ID CLASS_ID` | 指定 AI 课程 |
| `--noexam` / `--exam` | 跳过 / 启用 AI 测试 |
| `-l`, `--limit MINUTES` | 单门课程时限，0 表示不限 |
| `-s`, `--speed NUMBER` | 播放速度，必须为有限正数 |
| `-t`, `--threshold NUMBER` | 完成阈值，必须为有限正数 |
| `--tree-view` / `--no-tree-view` | 开启 / 关闭课程树 |
| `--progressbar-view` / `--no-progressbar-view` | 开启 / 关闭进度条 |
| `--image-path PATH` | 保存登录二维码 |
| `--proxy URL` | 设置代理 |
| `-d`, `--debug` | 开启调试日志 |
| `-h`, `--help` | 查看完整参数 |

布尔参数接受 `true/false`、`yes/no`、`1/0`。保留 `--tree_view`、`--progressbar_view`、
`--show_in_terminal`、`--image_path` 和 `--noexam False` 等原有写法。

## 配置字段

| 字段 | 用途 |
| --- | --- |
| `username`, `password`, `qrlogin` | 登录方式与账号配置 |
| `save_cookies` | 是否保存并复用登录态 |
| `proxies` | Requests 代理配置 |
| `tree_view`, `progressbar_view`, `qr_extra`, `image_path` | 终端及二维码显示 |
| `logLevel` | 日志等级 |
| `pushplus`, `bark` | 可选通知服务 |
| `ai.enabled`, `ai.use_zhidao_ai` | 是否启用 AI、是否使用平台内置 AI |
| `ai.openai` | 外部 AI 地址、密钥与模型 |
| `ai.max_exam_attempts` | 每个知识点最多尝试次数，默认 5 |
| `ai.ppt_processing` | PPT 转文本服务与清理策略 |

跳过测试时不校验未使用的 AI 服务配置。外部 AI 的 `api_base` 支持带或不带 `/v1`。
PPT 服务使用 `moonShot.base_url`；`delete_after_convert` 控制转换后删除本项目上传的文件。
缓存清理只使用本地上传清单，不扫描账号的其他远程文件。

## 课程清单

```json
[
  {"name": "共享课", "id": "COURSE_SECRET", "type": "zhidao"},
  {"name": "校内课", "id": "COURSE_ID", "type": "hike"},
  {"name": "AI 课", "id": "COURSE_ID", "classId": "CLASS_ID", "type": "ai"}
]
```

旧清单中仅有 `id` 的普通课程仍然可用。空清单表示不执行任何课程。
任一课程来源获取失败时，`--fetch` 不覆盖原清单。

## 日志与退出状态

日志位于 `var/logs/`，每个文件达到 5 MiB 后轮转，保留 3 份备份。
调试模式沿用项目现有行为，会记录请求参数和 Cookie。

结束时汇总完成、失败和跳过数量。失败返回 `1`，用户中断返回 `130`，正常结束或仅达到时限返回 `0`。
正在进行的网络请求仍受自身超时约束，因此课程时限不保证在精确到秒的时间点立即结束。
