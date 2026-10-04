# Codex WorkBuddy Bridge · intelmac

让 Codex.app 向本机 WorkBuddy 桌面应用分配任务，并收回结果。使用 WorkBuddy 的应用内置运行时与现有受保护登录配置。旧暖启动任务的订阅扣减已核对，本轮冷启动新用量仍待用户确认。

**v0.1.0-alpha.3：冷启动、权限、取消、故障及整个应用退出/重开真实验收通过。** 发布状态见仓库 Release；正式 v0.1.0 仍待 Codex 原生 MCP 加载更新代码、新用量核对及发布检查。安装说明见 [INSTALL](docs/INSTALL.md)。

## 部署后是什么

首版是 Codex 调用的 **本地 MCP 服务**。Codex 启动服务，服务通过 WorkBuddy 的本机内部通信通道管理任务；模型执行仍由 WorkBuddy 完成。用户在 Codex 对话框下达任务，无须另开桥接器窗口。

- MCP：实际执行启动、查询、等待、取消和收回结果。
- Skill：可选的使用说明，帮助 Codex 选择工具、等待和汇总结果。
- 独立桌面应用：后续可加状态菜单，首版不需要。

执行通道使用 WorkBuddy 应用内置运行时与本机 ACP。内部 sidecar 已运行时使用该通道；没有 sidecar 时，在任务启动时创建本项目自有 worker。用户在 Codex.app 中操作，旧任务用量页的使用端显示 CLI。冷启动设计见 [COLD_START](docs/COLD_START.md)。

## 已验证

- 2019 Intel Mac / macOS 26.5.1，WorkBuddy **5.6.2**；当前严格限制这个应用版本。
- GUI 已登录但没有 sidecar 时，双模型短任务与 1m 会话预算通过；预算设置不代表百万 token 满载测试。
- 文件允许一次、明确 Bash 命令允许一次、目录外 Bash 拒绝、流式取消通过，自有资源清理均确认。
- 新路径的错误本机凭证、worker 失联和 MCP 服务强制结束/重启通过；中断任务 uncertain，不自动重发。
- 整个 GUI 在流式输出及等待权限时正常退出/重开通过；权限等待解除、未授权文件未生成，旧请求幂等，新自有任务成功，清理确认。
- 自动测试结果见 [测试报告](docs/TEST_REPORT.md)；[云端自动测试](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/actions/workflows/tests.yml) 的结果以对应提交的运行记录为准。

只读 `health` 不启动任务。冷启动时 `ready_for_task=true`、`runtime_ready=false`、`authenticated=null` 表示具备启动条件且认证尚未验证；不应当成连接失败。实际任务成功后再确认执行与认证。现有用户 Skill 有加载证据，目标桌面 MCP 尚未继承。详细范围和待办见 [验收与差距](docs/AUDIT.md)。

## 文档

- [验收与差距](docs/AUDIT.md)：模型、上下文、工具继承和上游能力对照。
- [移植计划](docs/PLAN.md)：范围、步骤和验收条件。
- [桌面连接器适配](docs/CONNECTORS.md)：已验证的协议边界与后续实现顺序。
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

MCP 提供 `start_task`、`get_task`、`wait_task`、`cancel_task`、`get_result`、`health` 与 `respond_permission`。结果完整正文落盘，工具返回摘要与文件路径。`start_task` 可指定 `model` 和 `context_tokens`，默认模型为 `deepseek-v4.1-flash`；另一个主力模型为 `glm-5.3-flash`。指定 `context_tokens=1000000` 可选择已验证的 1m 会话预算。

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
