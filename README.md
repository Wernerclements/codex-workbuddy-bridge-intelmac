# Codex WorkBuddy Bridge · intelmac

让 Codex.app 向本机 WorkBuddy 桌面应用分配任务，并收回结果。使用 WorkBuddy 的应用内置运行时与现有受保护登录配置。旧暖启动任务已核对 2.26 积分；新冷启动路径从用户用量页核对 7 条、2.88 积分，其余已接受请求的消耗尚未确定。

**v0.1.2：适配 WorkBuddy 5.8.2。** 原生模块迁移、账号隔离认证和预热进程识别已修复；三模型、文件/权限/取消、故障以及两种整应用退出恢复验收通过。本机及 Intel macOS / Ubuntu 云端各 259 项回归通过。当前对话已有 MCP 进程需完全重启后确认 0.1.2。详见 [5.8.2 适配记录](docs/WORKBUDDY_5_8_2.md)，发布状态见 Release。

## 部署后是什么

首版是 Codex 调用的 **本地 MCP 服务**。Codex 启动服务，服务通过 WorkBuddy 的本机内部通信通道管理任务；模型执行仍由 WorkBuddy 完成。用户在 Codex 对话框下达任务，无须另开桥接器窗口。

- MCP：实际执行启动、查询、等待、取消和收回结果。
- Skill：可选的使用说明，帮助 Codex 选择工具、等待和汇总结果。
- 独立桌面应用：后续可加状态菜单，首版不需要。

执行通道使用 WorkBuddy 应用内置运行时与本机 ACP。内部 sidecar 已运行时使用该通道；没有 sidecar 时，在任务启动时创建本项目自有 worker。用户在 Codex.app 中操作，旧任务用量页的使用端显示 CLI。冷启动设计见 [COLD_START](docs/COLD_START.md)。

## 已验证

- 2019 Intel Mac / macOS 26.5.1，WorkBuddy **5.8.2**；保留历史 **5.6.2、5.7.7** 的独立映射，未降级重跑旧版桌面实测。其它版本默认拒绝任务。
- GUI 已登录但没有 sidecar 时，Deepseek、GLM、Step 三模型短任务通过；Step 确认 1m 会话预算，不代表百万 token 满载输入测试。
- 文件允许一次、指定 Bash 允许一次、目录外 Bash 拒绝、Deepseek 输出后取消，以及幂等与自有资源清理通过。
- 错误本机认证、worker 失联、MCP 服务强制中断/重启通过；中断任务 uncertain，不自动重发。
- 整个 GUI 在执行中、等待权限时正常退出/重开通过；权限等待解除、未授权文件未生成，重开后新任务成功，清理确认。
- 本轮账号用量未核对；历史用量与 MCP 边界分别见测试报告。
- 本机及 Intel macOS / Ubuntu 云端各 **259 项自动测试**通过，详见 [测试报告](docs/TEST_REPORT.md) 与 [云端检查](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/actions/workflows/tests.yml)。

只读 `health` 不启动任务。冷启动时 `ready_for_task=true`、`runtime_ready=false`、`authenticated=null` 表示具备启动条件且认证尚未验证；不应当成连接失败。实际任务成功后再确认执行与认证。现有用户 Skill 有加载证据；部分 MCP 已实际调用成功，通达信、同花顺和目标法律工具仍未发现。详见 [三模型 MCP 实测](docs/MCP_TEST.md) 和 [验收与差距](docs/AUDIT.md)。

## 文档

- [5.8.2 适配记录](docs/WORKBUDDY_5_8_2.md)：本次模块、认证、预热进程变化及验收边界。

- [5.7.7 适配记录](docs/WORKBUDDY_5_7_7.md)：本次修复、三模型任务、恢复验收和限制。

- [验收与差距](docs/AUDIT.md)：模型、上下文、工具继承和上游能力对照。
- [移植计划](docs/PLAN.md)：范围、步骤和验收条件。
- [桌面连接器适配](docs/CONNECTORS.md)：已验证的协议边界与后续实现顺序。
- [三模型 MCP 实测](docs/MCP_TEST.md)：Deepseek、GLM、Kimi 的真实调用、依赖错误和账号用量边界。
- [测试报告](docs/TEST_REPORT.md)：成功、失败及待验证项。
- [版本兼容](docs/COMPATIBILITY.md)：哪些环境实际测过。
- [冷启动设计](docs/COLD_START.md)：原生握手、自有资源与更新维护。
- [上游来源](reference/README.md)：固定版本、许可证与独立补丁。

## 安装

```sh
python3 scripts/install.py --dry-run
python3 scripts/install.py
```

Intel 原生依赖缺少预编译包时，在已有编译工具的环境中使用 `--allow-source-build`。安装器备份并合并 Codex 配置，详见 [安装说明](docs/INSTALL.md)。首次安装后重新加载 Codex MCP 或重启应用。

MCP 提供 `start_task`、`get_task`、`wait_task`、`cancel_task`、`get_result`、`health` 与 `respond_permission`。结果完整正文落盘，工具返回摘要与文件路径。`start_task` 可指定 `model` 和 `context_tokens`，默认模型为 `deepseek-v4.1-flash`；另一个主力模型为 `glm-5.3-flash`；本次新增 `step-5-preview` 真实短任务验收。指定 `context_tokens=1000000` 可选择已验证的 1m 会话预算。

## 重现测试

需要 Python 3.12 和系统 `patch`。在独立环境安装测试依赖：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-test.lock
.venv/bin/python -m pip check
.venv/bin/python scripts/test_upstream.py
.venv/bin/python scripts/test_bridge.py
```

运行器核对上游文件哈希，在临时副本中应用补丁，再执行完整测试。详细输出只保存在忽略的 `.local/`。

在已安装并启动 WorkBuddy 的 Mac 上运行只读诊断：

```sh
python3 scripts/probe_workbuddy.py
```

诊断不发送任务、不创建会话、不改写 WorkBuddy 历史或 Codex 配置。只输出版本和状态，不输出密码或会话正文。暂时要求应用位于 `/Applications/WorkBuddy.app`；其他位置属于后续适配范围。

## 项目边界

首版连接本机 WorkBuddy 内置运行时，串行执行任务并保存自己的记录。桌面原生历史、已有专家与会话上下文不会自动继承。Trae、Qoder、已有会话续接和多任务并发以后再评估。内部通道不是公开稳定 API，应用更新可能需要修复；支持情况以兼容表和真实验收记录为准。

本仓库不包含 WorkBuddy 的私有程序代码、账号数据或凭证。上游 Windows 代码保留在 `reference/upstream/`，仅供对照和回归测试。

## 许可证

MIT。上游版权及许可证完整保留，见 [LICENSE](LICENSE) 与 [上游许可证](reference/upstream/LICENSE)。
