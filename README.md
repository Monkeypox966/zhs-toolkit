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

首次运行会生成 `config/config.json`。默认使用二维码登录，已有有效 Cookie 时自动复用。
`--check` 仅检查登录和课程列表。

## 常用命令

```powershell
uv run zhs --fetch
uv run zhs -c COURSE_ID
uv run zhs -c COURSE_ID -v VIDEO_ID
uv run zhs -ai COURSE_ID CLASS_ID --noexam
uv run zhs --no-tree-view --no-progressbar-view
uv run zhs --help
```

也可以通过 `uv run python -m zhs` 或 `uv run python main.py` 启动。
完整参数见 [使用说明](docs/usage.md)。

## 目录

| 路径 | 职责 |
| --- | --- |
| `src/zhs/` | 应用入口、配置与任务流程 |
| `src/zhs/clients/` | 课程、AI、试卷和资料客户端 |
| `src/zhs/common/` | 传输、存储、日志及协议工具 |
| `tests/` | 自动化测试 |
| `config/config.example.json` | 配置示例 |
| `config/config.json` | 本地账号配置，不纳入版本管理 |
| `var/` | 登录态、清单、缓存、日志和构建产物，不纳入版本管理 |
| `docs/` | 使用与开发文档 |

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

## 运行结果

任务结束时汇总完成、失败和跳过数量。退出码 `0` 表示正常结束或达到时限，`1` 表示存在失败，
`130` 表示用户中断。网络等待受各请求的超时设置约束。

## 许可证

[MIT License](LICENSE)。
