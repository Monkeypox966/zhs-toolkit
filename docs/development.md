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
- `automation` 驱动默认恢复循环、退避等待、课程补发现和已评分试卷恢复；`--once` 保留单轮执行。
- `clients/course` 提供会话、登录和统一入口；`shared`、`campus`、`ai_courses` 分别实现三类课程协议。
- `clients/exam` 管理完整试卷和经验证的答案；`ai` 与 `materials` 管理模型请求和资料服务。
- `journal` 保存课程/视频级任务及每次尝试的结果；`common/contracts` 校验外部数据；`common/locking` 提供进程锁。
- `common/checkpoints` 使用任务局部上下文连接试卷提交与运行记录，提交请求只能在检查点持久化成功后发送。
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
`uv run zhs --check` 可单独执行真实连接检查。`tests/fixtures/` 保存人工构造的接口响应样例，
包含多分段试卷和题目；异常结构、缓存并发合并和真实跨进程锁也有回归测试。
`test_recovery` 还使用真实子进程突然退出，验证提交检查点、运行记录并发排他和旧实例写入保护，
并覆盖磁盘写入失败、提交后评分缺失、下载截断和锁等待中断等边界。
`test_automation` 覆盖无人值守恢复、延迟评分只读确认、目录补发现、进程重启后的计划复用和平台进度复查。
`test_authentication` 覆盖登录请求编码、拒登状态、SSO 续期、提交拒绝与未知结果的区别，以及待确认知识点隔离。
`test_control` 使用真实子进程验证停止、默认启动接管、提交边界延迟停止、等待超时和旧停止请求隔离。

运行控制通过 `common/control.py` 提供，同一数据目录的 `control.lock` 串行化停止和接管，
`runner.lock` 作为存活及互斥依据。`runner.json` 保存 PID 和本次实例的随机标识，
`stop.json` 仅对标识匹配的实例生效；不能仅凭 PID 或残留元数据判断进程存活。
业务通过 `check_stop()` 和可中断等待检查停止请求，使用 `StopRequested` 展开现有中断清理。
提交及结果落盘期间使用 `defer_stop()`，避免停止命令在发送和保存确认之间制造不确定状态。

修改平台适配器时应补充响应样例，先通过契约校验再进入业务流程。暂时网络失败可以有限重试，
但提交结果不明确时不得盲目重发。新增任务选项必须同步维护运行记录的白名单校验。
运行记录的公开 `save()` 和 `execute()` 使用同一个进程锁并校验修订标识；
持锁执行过程中的检查点通过内部写入保存。不要从其他上下文直接调用内部写入方法。
扩展不可逆操作时应保持“落盘后发送、确认后推进状态”的顺序，未完成状态不得按成功处理。

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
| `var/runs/` | 任务计划、尝试历史与续跑状态 |
| `var/locks/` | CLI 执行锁 |

## 本地版本管理

仓库使用 `main` 分支。提交源码、测试、配置示例、文档与依赖锁文件，
本地配置、运行数据、虚拟环境和构建缓存由 `.gitignore` 排除。

验证通过后创建本地提交；远程仓库单独配置。
