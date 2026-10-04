---
name: workbuddy-bridge
description: Delegate a task from Codex to the existing WorkBuddy desktop application using the local workbuddy_intelmac MCP bridge, then collect the result.
---

# WorkBuddy 协作

在用户希望把明确任务交给 WorkBuddy 桌面应用，并由 Codex 收回结果时使用。要求 MCP 服务 `workbuddy_intelmac` 已安装并连接。

## 委派

1. 调用只读 `health` 确认 WorkBuddy 状态。冷启动时 `ready_for_task=true`、`runtime_ready=false`、`authenticated=null` 表示可以启动自有任务，认证尚未验证；不要求先有 sidecar。`available=false` 时说明诊断结果。不要自行读取应用凭证或改用模型 API。
2. 将目标、所需输入、输出文件和完成标准写成清楚的任务说明。使用独立工作目录，避免与用户正在进行的会话或 Codex 当前修改相互影响。
3. 调用 `start_task(prompt, cwd, request_id, model, context_tokens)`，为本次委派提供唯一、简短的 ASCII 标识，并保留返回的 `task_id`。响应丢失时使用同一标识和完全相同参数查询原记录；服务不会重新提交。不同任务使用不同标识。`cwd` 必须是已经存在的目录。首版串行执行一个任务。
4. 用 `get_task(task_id)` 查询状态，或用 `wait_task(task_id, timeout_seconds)` 等待。单次等待最多 60 秒，给用户正常进度反馈。
5. 等待超时不表示任务失败，也不表示可再次提交；继续查询原任务 ID。断线或服务重启后，先查询原任务的已保存状态，避免重复执行。

## 权限与取消

- 不请求或默许 `fullAccess`、永久允许或无条件授权。状态为 `permission_required` 时，向用户说明 `permission_request` 中的操作、范围和可选项，展示 `input` 中的具体命令或参数及 `locations` 中涉及的路径。用户明确选择后，调用 `respond_permission(task_id, request_id, option_id)`。`request_id` 使用当前请求的 ID，`option_id` 使用所选项的 `optionId`。
- 只响应用户明确授权的权限请求。若当前 MCP 未提供可用的响应接口，保留等待状态，告诉用户需要处理的请求。
- 用户取消时调用 `cancel_task`，之后查询最终状态；取消请求被接收不等于任务已终止。
- 只操作本桥接器创建的任务，不接管其它 WorkBuddy 会话，不停止整个 WorkBuddy 应用。

## 回收结果

任务进入终态后调用 `get_result(task_id)`。读取 `answer_path` 指向的正文，核对 `status`、`artifacts` 文件清单、`partial`、`scan_limited`、`cleanup_pending` 和 `cleanup_confirmed`。长文本从结果文件读取，不仅依赖状态摘要。`partial=true` 表示正文或文件只保留了部分执行结果，必须说明尚未确认完整完成；扫描受限时，说明文件清单可能不完整。

`uncertain` 表示执行结果未确认，不要自动重发；使用原 task_id 查询或使用完全相同 request_id 与参数取回记录。清理未确认时报告实际标记，不把任务终态当作资源清理已完成。应用更新后当前适配仅允许已验证的 5.6.2，不尝试绕过版本检查。

核对实际产物和用户的完成标准。报告任务是成功、失败、取消还是结果不确定。保留任务 ID 和可点击的结果路径。模拟测试通过、健康检查通过或账号已认证，都不能作为真实任务完成的证据。

依据真实任务和 WorkBuddy 账号用量页报告订阅复用。v0.1.0 的新冷启动路径已有 7 条记录，合计 2.88 积分；其余已接受请求的消耗未知，不能推算为零。旧暖启动路径另有 4 条、2.26 积分。未经核对，不声称没有额外费用。

## 模型、上下文与工具边界

当前主力模型为 `deepseek-v4.1-flash`（默认）和 `glm-5.3-flash`。根据用户选择显式传入模型；长资料任务可指定 `context_tokens=1000000`，未指定时沿用该模型默认预算。相同 request_id 重试必须保留模型和上下文参数。核对结果中的 model.selection_confirmed、model.context_tokens 与 usage.context_size；不要把配置确认称为百万 token 长输入实测。

用户 Skill 已有真实加载证据；通达信、同花顺、法律等桌面连接器目前未可靠继承。先以实际工具发现和完成事件核对，不根据历史缓存或界面启用状态宣称可用，不向用户承诺调用不存在的工具。

初始化和 health 可跨对话；任务操作仍由一个服务持有记录目录。出现锁冲突时说明限制，不创建另一任务目录规避并发规则，不关闭其他对话服务。
