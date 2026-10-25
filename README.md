# ZHS Toolkit

智慧树课程命令行工具，支持共享课、校内课和 AI 课程。扫码登录后自动发现课程，中断后可以继续。

<br>

## 第一步：安装运行环境

先安装 [uv](https://docs.astral.sh/uv/getting-started/installation/)，由它管理 Python 和项目依赖。
已经安装 uv 的用户可以直接执行本节最后的 Python 安装命令。

**Windows**：打开 PowerShell，执行：

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux**：打开终端，执行：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

安装完成后，关闭并重新打开终端，再执行：

```sh
uv --version
uv python install 3.14
```

<br>

## 第二步：下载项目

如果已安装 Git，执行：

```sh
git clone https://github.com/Monkeypox966/zhs-toolkit.git
cd zhs-toolkit
```

没有 Git 也可以 [下载 ZIP](https://github.com/Monkeypox966/zhs-toolkit/archive/refs/heads/main.zip)，
解压后在包含 `pyproject.toml` 的文件夹中打开终端。
Windows 可以在文件夹空白处右键，选择“在终端中打开”。

<br>

## 第三步：启动并扫码登录

在项目文件夹中执行：

```sh
uv run zhs
```

首次启动会安装依赖，请等待安装完成。出现二维码后，用智慧树 App 扫码并在手机上确认登录，
无需提前填写账号密码。Windows 和 macOS 默认弹出二维码窗口，其他情况会尝试在终端显示。

登录成功后，程序会发现全部课程并开始处理，自动保存进度、重试失败项，并复查平台完成状态。
运行期间请保持终端打开。默认也会处理 AI 课程测试；如果只想处理学习资源、跳过测试，使用：

```sh
uv run zhs --noexam
```

<br>

## 第四步：停止与下次继续

在运行窗口按 **Ctrl+C** 可以中断。也可以另开终端，进入同一个项目文件夹后执行：

```sh
uv run zhs stop
```

下次回到项目文件夹，仍然运行下面这一条命令，会自动恢复未完成的任务：

```sh
uv run zhs
```

有效登录态会自动复用；需要重新登录时会再次显示二维码。
如果旧实例还在运行，再次启动会先通知它保存进度并退出，然后接着运行。

<br>

## 其他常用操作

| 想做什么 | 命令 |
| --- | --- |
| 只检查登录和课程列表，不执行学习任务 | `uv run zhs --check` |
| 导出课程清单及 ID 到 `var/execution.json` | `uv run zhs --fetch` |
| 只处理指定课程 | `uv run zhs -c COURSE_ID` |
| 只处理指定 AI 课程，并跳过测试 | `uv run zhs -ai COURSE_ID CLASS_ID --noexam` |
| 只执行一轮，不持续重试 | `uv run zhs --once` |
| 只生成任务计划 | `uv run zhs --dry-run` |
| 查看运行历史 | `uv run zhs --history` |
| 查看全部参数 | `uv run zhs --help` |

把 `COURSE_ID`、`CLASS_ID` 换成课程清单里的实际值；日常处理全部课程时无需填写。

<br>

## 常见问题

**找不到 `uv` 命令？** 安装后先重新打开终端；仍不可用时，按 [uv 安装说明](https://docs.astral.sh/uv/getting-started/installation/) 检查安装。

**二维码窗口没出现？** 改用 `uv run zhs --show-in-terminal`，在终端扫码。二维码过期后会自动刷新。

**一直显示等待或重试？** 程序会对失败项和待确认结果继续复查。需要临时退出时按 Ctrl+C；
之后再次运行即可继续。平台验证、网络故障或评分未返回仍可能阻止完成，以终端提示和平台实际记录为准。

**配置和数据在哪里？** 首次运行会生成 `config/config.json`。登录态、日志、缓存和运行记录保存在 `var/`。
默认扫码登录无需修改配置；外部 AI、通知等可选设置见 [完整使用说明](docs/usage.md)。
本地配置和运行数据不会随仓库上传。

更多参数、报告和恢复规则见 [完整使用说明](docs/usage.md)；参与开发见 [开发说明](docs/development.md)。

<br>

## 来源与许可证

本项目基于 [VermiIIi0n/fuckZHS](https://github.com/VermiIIi0n/fuckZHS) 深度修改、重构和优化，
以 **ZHS Toolkit** 独立维护。主要改进包括模块化结构、AI 课程处理、扫码登录恢复、任务续跑和运行报告。
感谢原作者与上游贡献者。

使用 [MIT License](LICENSE)，保留原作者版权声明；修改部分归相应贡献者所有。
