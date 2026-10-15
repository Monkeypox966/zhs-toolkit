# 开发说明

## 环境与命令

Python 版本由 `.python-version` 和 `pyproject.toml` 声明，依赖统一维护在 `pyproject.toml`，
精确解析结果提交到 `uv.lock`。使用 `uv sync --locked` 安装项目及开发依赖。

```powershell
uv run ruff check --no-cache .
uv run ruff format --check .
uv run python -B -m unittest discover -s tests -q
uv build --out-dir var/build
```

应用提供 `zhs` 命令、`python -m zhs` 和根目录 `main.py` 三种启动入口。
导入 `zhs`、`zhs.app` 不登录、不发请求、不读写配置。

## 职责边界

- `app` 组合参数、配置、客户端和任务流程，并决定退出码。
- `cli` 只解析参数；`config` 负责配置迁移和校验；`paths` 管理所有本地路径。
- `workflow` 统一发现三类课程、执行任务、汇总结果。
- `clients/course` 保留平台协议和播放逻辑；其余客户端管理 AI、试卷和资料服务。
- `common` 提供传输、原子写入、日志、协议签名和数据容器。

推荐公开入口：

```python
from zhs import ZHSClient

client = ZHSClient()
try:
    # 登录后调用需要的课程接口。
    pass
finally:
    client.close()
```

集成代码使用 `zhs` 包路径，并在使用结束后关闭客户端。

## 测试

测试按参数配置、任务流程、课程、试卷与 AI、资料、基础设施分类。
外部服务使用 mock，临时文件使用 `TemporaryDirectory`；测试不得依赖真实账号或实际提交。
`uv run zhs --check` 可单独执行真实连接检查。

## 数据目录

项目内运行时，配置位于 `config/`，运行数据位于 `var/`。二者不作为 Python 包内容构建。
独立安装时使用 `~/.zhs/`；`ZHS_HOME` 可以覆盖数据根目录，与启动时的工作目录无关。

| 路径 | 内容 |
| --- | --- |
| `config/config.json` | 本地配置 |
| `var/cookies.json` | 登录态 |
| `var/execution.json` | 课程清单 |
| `var/cache/answers/` | 答案缓存 |
| `var/cache/materials/` | 下载与转换缓存 |
| `var/logs/` | 轮转日志 |
| `var/build/` | 构建产物 |

## 本地版本管理

仓库使用 `main` 分支。提交源码、测试、配置示例、文档与依赖锁文件，
本地配置、运行数据、虚拟环境和构建缓存由 `.gitignore` 排除。

验证通过后创建本地提交；远程仓库单独配置。
