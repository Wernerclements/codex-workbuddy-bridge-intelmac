---
name: workbuddy-bridge
description: Delegate a task from Codex to the existing WorkBuddy desktop application using the local workbuddy_intelmac MCP bridge, then collect the result.
---

# WorkBuddy 协作

在用户希望把明确任务交给 WorkBuddy 桌面应用，并由 Codex 收回结果时使用。要求 MCP 服务 `workbuddy_intelmac` 已安装并连接。

## 委派

1. 调用 `health` 确认 WorkBuddy 连接和协议能力。不可用时说明诊断结果，不改用模型 API、不读取应用凭证。
2. 将目标、所需输入、输出文件和完成标准写成清楚的任务说明。使用独立工作目录，避免与用户正在进行的会话或 Codex 当前修改相互影响。
3. 调用 `start_task(prompt, cwd)`，保留返回的 `task_id`。`cwd` 必须是已经存在的目录。首版串行执行一个任务。
4. 用 `get_task(task_id)` 查询状态，或用 `wait_task(task_id, timeout_seconds)` 等待。单次等待最多 60 秒，给用户正常进度反馈。
5. 等待超时不表示任务失败，也不表示可再次提交；继续查询原任务 ID。断线或服务重启后，先查询原任务的已保存状态，避免重复执行。

## 权限与取消

- 不请求或默许 `fullAccess`、永久允许或无条件授权。状态为 `permission_required` 时，向用户说明 `permission_request` 中的操作、范围和可选项；用户明确选择后，调用 `respond_permission(task_id, request_id, option_id)`。`request_id` 使用当前请求的 ID，`option_id` 使用所选项的 `optionId`。
- 只响应用户明确授权的权限请求。若当前 MCP 未提供可用的响应接口，保留等待状态，告诉用户需要处理的请求。
- 用户取消时调用 `cancel_task`，之后查询最终状态；取消请求被接收不等于任务已终止。
- 只操作本桥接器创建的任务，不接管其它 WorkBuddy 会话，不停止整个 WorkBuddy 应用。

## 回收结果

任务进入终态后调用 `get_result(task_id)`。读取 `answer_path` 指向的完整正文，核对 `status`、`artifacts` 文件清单及 `scan_limited`。长文本从结果文件读取，不仅依赖状态摘要；扫描受限时，说明文件清单可能不完整。

核对实际产物和用户的完成标准。报告任务是成功、失败、取消还是结果不确定。保留任务 ID 和可点击的结果路径。模拟测试通过、健康检查通过或账号已认证，都不能作为真实任务完成的证据。

首次验证订阅复用时，依据真实任务和 WorkBuddy 的账号、用量记录报告；未经核对，不声称已使用订阅额度或没有额外费用。
