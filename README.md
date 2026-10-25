# ZHS Toolkit

一个基于 Python 的智慧树课程命令行工具，支持课程发现、资源处理、学习流程执行与结果汇总。

## 项目来源与改进

本项目基于 [VermiIIi0n/fuckZHS](https://github.com/VermiIIi0n/fuckZHS) 深度修改、重构和优化，
以 **ZHS Toolkit** 作为当前版本的名称，由 [Monkeypox966](https://github.com/Monkeypox966) 维护。
感谢原作者及上游贡献者提供的课程接口适配与基础实现；本仓库为独立维护的衍生版本。

当前版本的主要改进包括：

- 重构为 `src/zhs/` 模块化结构，使用 uv 管理依赖、命令行入口和构建。
- 整合共享课、校内课与 AI 课程，补充试卷、答案校验及资料缓存处理。
- 增加持久化任务计划、断点续跑、失败恢复、运行报告和平台进度复查。
- 优化扫码登录、登录过期恢复、单实例接管与停止操作。
- 增加接口契约校验、文件原子写入、进程锁、缓存配额和自动化回归测试。

原项目与本项目均使用 MIT 许可证，保留原作者版权声明。详细使用与开发说明见下文及 `docs/`。

## 环境

- Python 3.14 或更高版本
- uv，用于依赖管理、运行与构建

## 安装与启动

```powershell
git clone https://github.com/Monkeypox966/fuckZHS.git
cd fuckZHS
uv run zhs
```

日常只需要这一条命令。已有有效登录态时直接继续；需要登录时自动显示二维码，手机确认后自动恢复任务。
Windows 和 macOS 默认弹出二维码窗口，无法打开时自动在终端显示；二维码过期会自动刷新。
首次运行会生成 `config/config.json`，默认扫码登录无需填写账号密码。
直接运行 `uv run zhs` 会发现全部课程，自动恢复未完成任务，循环处理失败项，并复查平台进度。
没有进展时逐步延长重试间隔，直到完成或按 Ctrl+C 停止；重启同一命令可继续原计划。
中断后仍然只需 `uv run zhs`，无需记住 `--resume`；启动时会明确提示自动恢复进度。
再次执行 `uv run zhs` 会先通知旧实例保存状态并退出，再在当前终端继续运行。
使用 `uv run zhs stop` 可以从另一个终端停止运行，包括后台实例，无需查找 PID。
`--check` 仅检查登录和课程列表。

## 常用命令

```powershell
uv run zhs --fetch
uv run zhs stop                      # 保存状态并停止正在运行的实例
uv run zhs -c COURSE_ID
uv run zhs -c COURSE_ID -v VIDEO_ID
uv run zhs -ai COURSE_ID CLASS_ID --noexam
uv run zhs --no-tree-view --no-progressbar-view
uv run zhs --dry-run                  # 只生成任务计划
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

源码及配置示例中的账号、密码和 API 密钥均为空。本地配置、Cookie、运行记录、日志、缓存和
环境文件由 `.gitignore` 排除；这些数据仅保存在本地，不随本仓库发布。

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
- 登录失效先尝试 SSO 续期，需要重新登录时自动扫码；配置凭据被拒时自动切换扫码，无需重启或添加参数。
- AI 提示文本使用本地字节预算控制长度，无需额外下载分词表；超长参考文本保留末尾题目和选项。
- 自动模式重新读取平台进度确认学习完成，默认完成阈值为 100%。
- AI 资源以平台完成标记为准；平台的视频完成规则为超过片段的 80%，不会把已完成的 81% 视频反复补看。汇总进度与资源状态不一致时保留提示。
- 提交前持久化检查点，提交后确认完整评分；中途退出或结果不明确时，续跑等待平台状态核实。
- 同一数据目录只允许一个执行实例；启动时先停止旧实例，确认锁释放后接管。运行记录、资料和答案缓存另有进程锁。
- 下载校验非空内容和未压缩响应的声明长度，分块检查总耗时；下载和转换文本支持本地配额与过期清理。
- 报告在执行前写入计划，结束后更新；已有非报告文件不会被覆盖，续跑汇总保留已完成的结果。

## 运行结果

任务结束时汇总完成、失败、待处理和跳过数量。退出码 `0` 表示正常结束或达到时限，`1` 表示存在失败或待处理项，
`130` 表示收到运行中断信号。网络等待受各请求的超时设置约束。

## 许可证

[MIT License](LICENSE)。原项目版权归 VermiIIi0n 所有，本项目修改部分归相应贡献者所有。
再分发时请保留许可证及原有版权声明。
