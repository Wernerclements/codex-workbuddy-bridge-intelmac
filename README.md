# Codex WorkBuddy Bridge · intelmac

让 Codex.app 向本机 WorkBuddy 桌面应用分配任务，并收回结果。使用 WorkBuddy 的应用内置运行时与登录配置；已通过用户账号用量页核对订阅扣减。

**开发版 v0.1.0-alpha.2：已完成本机安装与主要端到端验收，指定模型用量已核对，界面重新加载后的调用仍待确认。** 安装说明见 [INSTALL](docs/INSTALL.md)。

## 部署后是什么

首版是 Codex 调用的 **本地 MCP 服务**。Codex 启动服务，服务通过 WorkBuddy 的本机内部通信通道管理任务；模型执行仍由 WorkBuddy 完成。用户在 Codex 对话框下达任务，无须另开桥接器窗口。

- MCP：实际执行启动、查询、等待、取消和收回结果。
- Skill：可选的使用说明，帮助 Codex 选择工具、等待和汇总结果。
- 独立桌面应用：后续可加状态菜单，首版不需要。

执行通道使用 WorkBuddy 应用内置 CLI Host、sidecar 与 ACP，账号用量页显示 CLI。用户在 Codex.app 中操作；桥接器启动并管理应用自带的执行运行时。

## 已验证

- 本机 macOS 26.5.1 / x86_64，WorkBuddy 5.6.2 的 Intel 可执行文件存在。
- 可只读发现运行中的 sidecar，读取协议版本 6、会话信息及本机认证状态。
- 上游完整单元测试：原样 30/31 通过；路径兼容补丁后，加上 3 项回归测试，34/34 通过。
- [云端自动测试](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/actions/workflows/tests.yml)：Intel macOS 和 Ubuntu 执行完整项目与上游回归；当前本机 152/152 通过（118 项项目测试 + 34 项上游回归），云端以运行记录为准。
- 原版真实任务测试停在 Windows 实例发现逻辑，尚未向模型发送任务。

开发版已通过真实 MCP 文本、文件回收、等待超时、执行中取消、权限拒绝、错误凭证、worker 失联和桥接器重启验收。本轮新增双模型选择与 1m 上下文预算，实际任务通过；现有用户 Skill 可加载，桌面目标 MCP 仍未继承。详细证据、三项待办的严格状态和上游差距见 [验收与差距](docs/AUDIT.md)。

## 文档

- [验收与差距](docs/AUDIT.md)：模型、上下文、工具继承和上游能力对照。
- [移植计划](docs/PLAN.md)：范围、步骤和验收条件。
- [桌面连接器适配](docs/CONNECTORS.md)：已验证的协议边界与后续实现顺序。
- [测试报告](docs/TEST_REPORT.md)：成功、失败及待验证项。
- [版本兼容](docs/COMPATIBILITY.md)：哪些环境实际测过。
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
