# WorkBuddy 5.8.3 适配记录

记录日期：2026-10-19。对象：Intel Mac、WorkBuddy **5.8.3**、桥接 **0.1.3**。

**状态：本机适配与真实验收通过，0.1.3 已安装并通过独立 stdio health。** 本机自动回归为 229 项项目测试 + 34 项上游回归，共 263 项，无跳过；本轮没有运行云端 CI，不能用 5.8.2 的旧云端结果代表 5.8.3。本机验收已完成，分支进入 PR 审核与 CI 阶段；尚未合并或 Release。

## 版本适配核对

- 新增 `main/app-lifetime-job.js` 版本专属保护模块适配，沿用六个语义槽 `D/G/k/A/T/w`，旧版本映射保留。保护字段仍由应用内置运行时处理，`createIfMissing=false`，凭证不进入 Python 结果、日志或任务记录。这里只记录适配接口事实，不包含应用实现代码。
- 认证类型为 `workbuddy-desktop/account-scoped-external-link`，与 5.8.2 相同。`main/protocol.js` 与 `cli/bin/codebuddy` 的文件哈希与 5.8.2 一致；这不表示整个原生模块相同。本机 sidecar 仍为协议 6。
- host 识别、`ready_for_task` / `runtime_ready` / `authenticated` 的含义、产品缓存只读投影、模型选择及就绪门、应用自带 `--prewarm` 进程边界均复核通过，没有阻断项。
- 继续支持历史版本 5.6.2、5.7.7、5.8.2；这些旧版本没有在本轮降级重测。未知版本仍默认拒绝任务。

此处记录兼容性结论，不包含应用内部代码、解包内容、凭证、请求头或原始会话。

## Core 七项真实验收

所有真实任务串行执行，使用桥接常规状态目录与实例锁。每项任务的相同 `request_id` 重试均返回同一记录：`task_id`、`created_at`、`started_at`、`session_id`、`runtime.worker_id` 一致，重试状态与最终状态一致。七项都 `cleanup_confirmed=true`、`cleanup_pending=false`。

| 项目 | 终态 | 验收结果 |
| --- | --- | --- |
| Deepseek-V4.1-Flash (`deepseek-v4.1-flash`) | succeeded | 指定模型确认，短文本标记匹配；会话预算 300,000 |
| GLM-5.3-Flash (`glm-5.3-flash`) | succeeded | 指定模型确认，短文本标记匹配；会话预算 300,000 |
| Step-5-Preview (`step-5-preview`) | succeeded | 指定模型确认；会话设置与 `usage.context_size` 均为 1,000,000。任务是短文本，不代表百万 token 满载测试 |
| Write 虚构文件 | succeeded | 仅对匹配的测试路径和内容使用 `allow_once`；产物与清单一致 |
| Bash 虚构文件 | succeeded | 仅对精确测试命令使用 `allow_once`；产物与清单一致 |
| 工作目录外 Bash | cancelled | 明确 `reject_once`；外部测试文件未生成 |
| Deepseek 输出后取消 | cancelled | 已观察到输出后取消，`partial=true`；未把部分结果当作完成 |

## 故障、认证与资源清理

- 错误认证：独立自有 runtime 的无 prompt 探测用三个独立 HTTP 客户端和 cookie jar。正确凭证前后都返回 HTTP 200、`authEnabled=true`、`authenticated=true`；无效凭证请求发出前 cookie 数为 0，返回 HTTP 200、认证开启但 `authenticated=false`；runtime 清理确认。最初同一客户端复用 cookie 的失败报告保留在本机。审计确认：正确凭证请求建立的 gateway cookie 被后续无效凭证请求复用，端点按 cookie 认证而返回已认证；这是测试会话污染，不是生产认证缺陷。故障脚本中“捕获任意泛型异常即通过”的布尔值不单独作为错误认证证据。本项只核对现有行为，没有修改生产认证代码。
- 自有 worker 失联：原任务为 `uncertain`，WorkBuddy GUI 仍可用，自有资源清理确认。
- 自有 MCP 服务中断并重启：原任务为 `uncertain`，自有资源恢复清理确认，WorkBuddy GUI 仍可用。

## 整 GUI 退出与恢复

两轮均由 AppleScript 正常退出整个 WorkBuddy，没有向 GUI 或非自有进程发送强制终止信号。报告按 UTC 记录，以下为北京时间（UTC+8）：

| 场景 | 时间 | 结果 |
| --- | --- | --- |
| 任务执行中退出 | 2026-10-19 11:22:58–11:23:32 | 原任务 `uncertain`，同一记录重试，清理确认；重开后的 WorkBuddy 5.8.3 ready，新 owned worker 短任务成功并清理 |
| 权限等待中退出 | 2026-10-19 11:24:12–11:24:51 | 权限等待解除、测试文件不存在，原任务 `uncertain` 且同一记录重试；重开后新 owned worker 短任务成功并清理。开始时没有旧 sidecar |

两轮中重开后的新任务均成功；退出前任务和恢复后新任务分别核对幂等性。每轮 finally 清理的两个自有 worker 都确认清理。

## 安装与回归

- 源码、安装分发包与导入模块版本均为 `0.1.3`；安装器 dry-run 确认 Codex 配置无冲突，实际安装完成，`pip check` 显示无依赖问题。
- 从安装环境启动的新 stdio 服务返回 `bridge_version=0.1.3`、WorkBuddy `5.8.3`、`available=true`、`ready_for_task=true`、`runtime_ready=false`、`authenticated=null`。该 health 没有提交任务；没有 sidecar 时 `protocol=null` 是预期状态。主代理对当前对话原生 MCP 的只读 health 仍报告 `bridge_version=0.1.2`，WorkBuddy `5.8.3`，available/ready/supported=true，runtime_ready=false、authenticated=null，也未提交任务。发布后须完全重启 Codex，再只读确认原生 `bridge_version=0.1.3`。
- 安装后只读进程检查确认唯一 WorkBuddy 5.8.3 GUI 身份正常，未发现自有 worker 或 reaper 残留。
- 229 项项目测试与 34 项上游回归均在本机通过，无跳过；本轮云端未运行。

## 边界

本轮没有核对模型账号用量或费用，没有重跑其他 WorkBuddy 桌面 MCP 业务调用；#5（桌面连接器继承）和 #11（权限请求详情）仍开放。Step 1m 是会话预算设置和短任务的回显，不是百万 token 长输入测试。旧版本没有降级重测。Raw reports、模型正文和应用内部材料只留在忽略的本机 `.local/update-583/`，不提交、不上传。安装、health 与清理摘要保存在忽略目录 `.local/update-583/`。正式 Release 状态待后续审核与发布。
