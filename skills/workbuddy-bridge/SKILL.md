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

`uncertain` 表示执行结果未确认，不要自动重发；使用原 task_id 查询或使用完全相同 request_id 与参数取回记录。清理未确认时报告实际标记，不把任务终态当作资源清理已完成。应用更新后当前适配仅允许 5.6.2、5.7.7、5.8.2、5.8.3，不尝试绕过版本检查；5.7.6 未完成验收。

核对实际产物和用户的完成标准。报告任务是成功、失败、取消还是结果不确定。保留任务 ID 和可点击的结果路径。模拟测试通过、健康检查通过或账号已认证，都不能作为真实任务完成的证据。

依据真实任务和 WorkBuddy 账号用量页报告订阅复用。v0.1.0 的新冷启动路径已有 7 条记录，合计 2.88 积分；其余已接受请求的消耗未知，不能推算为零。旧暖启动路径另有 4 条、2.26 积分。未经核对，不声称没有额外费用。

## 模型、上下文与工具边界

当前主力模型为 `deepseek-v4.1-flash`（默认）和 `glm-5.3-flash`。根据用户选择显式传入模型；长资料任务可指定 `context_tokens=1000000`，未指定时沿用该模型默认预算。相同 request_id 重试必须保留模型和上下文参数。核对结果中的 model.selection_confirmed、model.context_tokens 与 usage.context_size；不要把配置确认称为百万 token 长输入实测。

用户 Skill 已有真实加载证据；部分现有 MCP 也已有实际执行证据：Deepseek 与 GLM 的 scansci-pdf 健康检查成功返回，三个模型的 sheetagent 解析因本机编辑器服务拒绝连接而失败。通达信、同花顺、法律等桌面连接器仍未可靠继承。先以实际工具发现、调用参数和返回核对，不根据历史缓存或界面启用状态宣称可用，不向用户承诺调用不存在的工具。

5.7.7 新增 Step-5-Preview，准确 ID 为 `step-5-preview`。真实短任务及 1m 会话预算通过；额外取消测试遇到服务端 429 队列满，不能说 Step 取消已验证，不自动换模型或重发。本轮扣费尚未核对。

Kimi K3 的已验证模型 ID 为 `kimi-k3-1`，本轮仅确认模型选择与 MCP 解析调用，未验证该模型的健康检查成功率。主力默认模型仍为 Deepseek/GLM。

延迟工具可能显示为 DeferExecuteTool 且权限 input 为空；不能凭包装器名称批准操作。先核对本任务自身原生记录中的真实工具和参数，只响应用户明确授权范围内的 allow_once。工具完成但正文包含 error 不算业务成功。sheetagent 的默认 session 可能对应真实用户文档，不在虚构文件测试授权内。

2026-10-05 三模型 MCP 测试用户核对首四条 21.65 积分，其中定向 Kimi 任务 18.99；后续三条用量未知，不能推断取消任务免费。详细记录见项目 docs/MCP_TEST.md。

初始化和 health 可跨对话；任务操作仍由一个服务持有记录目录。出现锁冲突时说明限制，不创建另一任务目录规避并发规则，不关闭其他对话服务。

0.1.1 查询终态任务时会在后台刷新清理状态，首次仍可能 pending；稍后查询原 task_id。不会重放任务。WorkBuddy 5.7.7 重开后，启动等待账号模型目录就绪，保护握手有界等待；health 的就绪字段不等于任务完成。更新源码后须完全重启 Codex 并只读确认 bridge_version。

0.1.2 / WorkBuddy 5.8.2：新原生保护模块与账号隔离认证按精确版本适配。应用自带 --prewarm CLI 不属于 GUI 或桥接自有进程，不接管或清理它。七项核心及三项故障实测通过；执行中及权限等待时整 GUI 退出恢复通过，正式状态见 Release。

0.1.3 / WorkBuddy 5.8.3：新增版本专属保护导出映射；认证类型、协议入口哈希与 5.8.2 一致，协议6及 host、ready、凭证、缓存、模型就绪与 prewarm 边界复核无阻断。七项 core 的状态、严格同记录幂等与清理、三项故障和两轮整 GUI 退出/重开均通过；错误认证由无 prompt、独立 cookie jar 验证，未修改生产认证。Step 的 1m 是短任务的会话预算确认，不是百万 token 满载测试。本机 263 项（229 项项目 + 34 项上游）通过；PR #19 GitHub Actions 在 Intel macOS 与 Ubuntu 各通过 263 项；费用未核对，MCP 业务未复验，#5/#11 仍开放。独立 stdio health 为 0.1.3；当前对话原生 health 仍为 0.1.2。发布后须完全重启 Codex 并只读确认原生 0.1.3。详见项目 docs/WORKBUDDY_5_8_3.md。
