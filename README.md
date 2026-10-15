# ZHS Toolkit

一个基于 Python 的智慧树课程命令行工具，支持课程发现、资源处理、学习流程执行与结果汇总。

## 环境

- Python 3.14 或更高版本
- uv，用于依赖管理、运行与构建

## 安装与启动

```powershell
uv sync --locked
uv run zhs --check
uv run zhs
```

首次运行会生成 `config/config.json`。已有有效 Cookie 时自动复用，否则自动模式使用配置中的账号密码登录。
直接运行 `uv run zhs` 会发现全部课程，自动恢复未完成任务，循环处理失败项，并复查平台进度。
没有进展时逐步延长重试间隔，直到完成或按 Ctrl+C 停止；重启同一命令可继续原计划。
需要二维码登录时显式使用 `--qrlogin`。
`--check` 仅检查登录和课程列表。

## 常用命令

```powershell
uv run zhs --fetch
uv run zhs -c COURSE_ID
uv run zhs -c COURSE_ID -v VIDEO_ID
uv run zhs -ai COURSE_ID CLASS_ID --noexam
uv run zhs --no-tree-view --no-progressbar-view
uv run zhs --dry-run                  # 只生成任务计划
uv run zhs --resume                   # 继续最近一次未完成任务
uv run zhs --retry-failed             # 重试失败及待处理任务
uv run zhs --report var/report.json   # 执行并导出报告
uv run zhs --history                  # 查看运行历史，无需登录
uv run zhs --once                     # 只执行一轮，不进入自动恢复循环
uv run zhs --help
```

也可以通过 `uv run python -m zhs` 或 `uv run python main.py` 启动。
完整参数见 [使用说明](docs/usage.md)。

## 目录

| 路径                           | 职责                                               |
| ------------------------------ | -------------------------------------------------- |
| `src/zhs/`                   | 应用入口、配置与任务流程                           |
| `src/zhs/clients/`           | 共享课、校内课、AI 课程、试卷和资料客户端          |
| `src/zhs/journal.py`         | 持久化任务状态、续跑与 JSON 报告                   |
| `src/zhs/common/`            | 传输、存储、日志及协议工具                         |
| `tests/`                     | 自动化测试                                         |
| `config/config.example.json` | 配置示例                                           |
| `config/config.json`         | 本地账号配置，不纳入版本管理                       |
| `var/`                       | 登录态、清单、缓存、日志和构建产物，不纳入版本管理 |
| `docs/`                      | 使用与开发文档                                     |

路径与启动目录无关。项目内默认使用 `config/` 和 `var/`；独立安装后默认使用 `~/.zhs/`。
可通过 `ZHS_HOME` 指定其他数据根目录。

## 开发与验证

```powershell
uv sync --locked
uv run ruff check --no-cache .
uv run ruff format --check .
uv run python -B -m unittest discover -s tests -q
uv build --out-dir var/build
```

架构与约定见 [开发说明](docs/development.md)。依赖声明维护在 `pyproject.toml`，精确版本由 `uv.lock` 锁定。

## 正确性与恢复

- 课程类型来自服务端元数据或显式清单，不根据 ID 字符猜测。
- 试卷读取全部分段；AI 和缓存答案校验当前选项、数量、重复项及题目内容变化。
- 优先使用有效缓存和 AI 答案；AI 不可用或答案不合法时随机作答，提交后缓存平台返回的有效正确答案并重试。
- 弹题保留到全部处理成功，并对暂时失败进行有限重试。
- 运行记录保存每次尝试的状态、错误、耗时和执行选项；中断后可续跑。
- 默认自动恢复，30 秒起步退避至最多 300 秒；完整评分恢复后继续任务，单个知识点失败不阻塞后续知识点。
- 自动模式重新读取平台进度确认学习完成，默认完成阈值为 100%。
- 提交前持久化检查点，提交后确认完整评分；中途退出或结果不明确时，续跑等待平台状态核实。
- 同一数据目录只允许一个 CLI 实例；运行记录、资料和答案缓存另有进程锁，旧记录实例不能覆盖新进度。
- 下载校验非空内容和未压缩响应的声明长度，分块检查总耗时；下载和转换文本支持本地配额与过期清理。
- 报告在执行前写入计划，结束后更新；已有非报告文件不会被覆盖，续跑汇总保留已完成的结果。

## 运行结果

任务结束时汇总完成、失败、待处理和跳过数量。退出码 `0` 表示正常结束或达到时限，`1` 表示存在失败或待处理项，
`130` 表示用户中断。网络等待受各请求的超时设置约束。

## 许可证

[MIT License](LICENSE)。
