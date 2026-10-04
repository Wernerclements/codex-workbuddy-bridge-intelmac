# 验收状态与上游差距

核对日期：2026-10-05（北京时间）。发布候选：`v0.1.0-alpha.3`，实际发布状态以 Release 为准。正式 `v0.1.0` 尚未发布。

## 三项待办的严格状态

| 待办 | 当前状态 | 已完成 | 剩余 |
| --- | --- | --- | --- |
| #1 单任务通道 | 已关闭 | Intel Mac 发现、应用内置自有运行时、真实文本及文件往返 | 原生桌面专家和任意旧会话不属于已实现能力 |
| #2 生命周期验收 | 已完成，随本次修复 PR 关闭 | 新冷启动双模型、文件/Bash 允许一次、目录外 Bash 拒绝、流式取消、错误本机凭证、worker 失联、MCP 强制重启；流式与权限等待时整 GUI 退出/重开、幂等、权限等待解除及清理 | 云端检查与 PR 合并 |
| #3 安装与发布 | 开放 | 本机安装、配置保留及备份、Skill、既有 alpha 发布流程；旧暖启动用量与旧版原生 health 确认 | Codex 原生 MCP 加载更新代码；用户核对本轮新用量；最终测试与正式发布 |

alpha.3 新路径完整真实矩阵已通过，#2 的旧冷启动缺口已有复验；本机 232 项自动测试通过，随本次修复 PR 关闭 #2。#3 尚有明确剩余工作，不能将整个里程碑记为完成。目标桌面 MCP 的 #5 仍开放，未继承。

## alpha.3 冷启动与整 GUI 恢复

应用已登录但没有 sidecar 时，只读 health 报告 ready_for_task，认证保持未验证；首次任务创建本项目自有 worker。原生受保护字段通过临时私有 Unix socket 握手，调用应用现有密钥辅助函数且 createIfMissing=false。不读取或输出裸 token、不关闭静态加密、不导出凭证、不重写账号。应用产品缓存由 launcher 只读加载并在 CLI 初始化前内联投影到自有进程，修复过早冻结的模型/工具快照；不改写缓存。应用更新后这些内部接口仍需适配和本机复验，见 [冷启动设计](COLD_START.md)。

Deepseek-V4.1-Flash 与 GLM-5.3-Flash 均完成真实短任务、模型回显和 1m 预算，清理确认；新路径文件 allow_once、明确 Bash allow_once、目录外 Bash reject_once 和流式取消通过；错误本机凭证、worker 失联及 MCP 服务 SIGKILL/重启也通过，清理确认。早期 standalone 的 refusal 对应 Authentication required，应归为认证问题，不能当成内容拒绝；模型目录失败的实验没有提交 prompt。

北京时间 23:46:22–23:46:49，初始没有 sidecar，正常退出整个 GUI 后已接受任务 uncertain 且 cleanup_confirmed；相同 request_id 返回原记录无重发。重开后新的 owned 运行时成功短任务，才确认认证，并完成新任务幂等和清理。整体 passed=true，无强杀非自有进程。10 月 5 日 00:00:08–00:00:40 的权限等待 GUI 退出补充验收同样通过；原任务 uncertain、partial=true、等待权限解除、申请文件未生成，幂等、新自有短任务和两个 worker 清理均确认。

本轮真实测试来自独立 MCP stdio，仍需 Codex 原生 MCP 加载本轮代码。新路径未配置独立模型 API；成功云端任务不能代替用户用量核对，新的账号归属与积分扣减待确认。以下旧四次任务 2.26 积分仅证明当时的暖启动路径。当前自动测试最终记录见 [测试报告](TEST_REPORT.md)，早期数量不是本轮合并结果。

## 两个主力模型与上下文

真实任务通过 MCP stdio 提交，先核对模型目录、选择并确认指定模型，再核对上下文选项。没有配置独立模型 API。

| 模型 | 模型 ID | 默认预算 | 可选预算 | 本次确认 |
| --- | --- | --- | --- | --- |
| Deepseek-V4.1-Flash | deepseek-v4.1-flash | 300,000 | 300k / 600k / 1m | 任务成功；usage_update.size=1,000,000 |
| GLM-5.3-Flash | glm-5.3-flash | 300,000 | 300k / 600k / 1m | 任务成功；usage_update.size=1,000,000 |

`start_task` 新增 `model` 与 `context_tokens`。默认模型为 Deepseek-V4.1-Flash；未指定上下文时使用应用该模型的默认预算，长资料任务可指定 `context_tokens=1000000`。同一 request_id 的模型或上下文改变会被拒绝，不能静默重发。

上下文设置是 session 内存覆盖，模型参数在 worker 内使用；不修改桌面全局默认模型。自动压缩窗口与上下文预算分别处理，1m 设置不是关闭压缩或 Max 模式。单次 prompt 上限仍为 100000 字符；本次短任务只验证设置与运行时预算，没有测试接近百万 token 的长输入承载或质量。

旧暖启动验收中，用户于 2026-10-04 提供 WorkBuddy 用量页记录并确认消耗正常：北京时间 16:07、16:10、16:20 的 Deepseek-V4.1-Flash 分别扣减 0.60、0.61、0.51 积分，共 1.72；16:08 的 GLM-5.3-Flash 扣减 0.54 积分，合计 2.26。时间和模型与本次四项真实测试对应，使用端显示 CLI。结合此前账号归属与无额外计费提示的确认，两个主力模型当时的订阅复用验收通过；本轮 alpha.3 新路径用量仍待核对。

## MCP 与 Skill 继承（alpha.2 证据，缺口仍存在）

- 两个模型都实际调用了 ToolSearch 和 Skill；Skill 加载现有 find-skills。工具调用事件与完成状态可核对。
- 补充读取本次测试任务自身的原生 CLI 历史：Skill 结果的 Base directory 精确指向 ~/.workbuddy/skills/find-skills，继承有原生结果证据；ToolSearch 返回1个相关候选 mcp__sheetagent__search_data，四个目标工具均不匹配。不会把任意 found 状态当作目标工具已发现。
- 原生 Skill 工具返回渲染后的正文，不含 YAML frontmatter；文中版本表 v1.7 与原文件 frontmatter 1.7.0 的表现不同是正常渲染行为。
- 默认桥接会话没有发现通达信、同花顺和目标法律工具。
- 桌面当前连接器启用状态包含这些服务。桥接目前没有采用桌面按会话生成 MCP 配置的投影步骤。
- 实验性地只为测试 worker 显式加载现有 mcp.json 后，仍未发现目标工具；不能简单通过传配置文件补齐。
- 没有调用交易、修改自选、发消息或其他第三方业务操作。本次工具发现不能当作第三方调用验收通过。

当前版本进一步验证了 WBIPC 的本机认证握手和只读协议清单：protocol=1，唯一 pipe 为 wb.request，唯一方法为 http.fetch。该通道是认证云端请求转发，不暴露 mcpList 或会话 MCP 配置投影。没有发送业务请求、提交模型任务或读取第三方数据。桌面会话的工具投影还需要独立适配。

缺口单独记录在 [#5 继承 WorkBuddy 桌面连接器](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/issues/5)。历史缓存和界面启用状态不能替代桥接当前会话的真实工具发现与只读调用。后续步骤见 [桌面连接器适配](CONNECTORS.md)。

## Codex 多对话初始化问题（既有修复与待更新加载）

旧实现启动即创建 Manager 并独占全局任务目录；第一个对话启动成功后，其他对话握手关闭。现改为首次任务操作时创建 Manager，初始化、工具发现与 health 不再拿任务锁。

已在原任务服务持锁时，实际启动两个 stdio 客户端，均完成初始化、7工具发现与 health。此前当前对话缺少原生入口，且设置没有重载/开关；用户于 2026-10-04 在初始化修复后完全退出并打开 Codex.app。随后唯一一次原生 workbuddy_intelmac.health 成功，available、authenticated、supported 均为 true，WorkBuddy 5.6.2、协议6。旧版原生工具加载验收通过，发生在首次退出测试前；alpha.3 仍需更新代码后的原生调用验收。任务操作仍由单个服务持有记录目录；未实现跨对话共享后台调度器，遇到冲突明确报告，不能将初始化修复称为多任务并发已实现。

## 与上游逐项对照

上游对照以研究时固定提交 `c00a8e5eab809a97963277e71ec008abb3d372ae` 为基准，不将该记录作为未来 main 未变化的保证。

| 能力 | 上游 | Intel Mac 当前版 | 差距/选择 |
| --- | --- | --- | --- |
| 单任务启动、等待、查询、取消 | 有 | 有，真实验收 | 核心通道覆盖，工具名称不同 |
| 执行通道 | 应用内置 CLI Host + sidecar + ACP | 已运行通道及原生受保护字段启动自有 worker；本机 ACP | 新增没有 sidecar 时的冷启动；均非任意 GUI 专家直接控制 |
| 模型选择 | 有 model 参数 | 本轮已实现并实测双模型 | 已补齐；默认模型按用户主力选择 |
| 推理强度 | reasoning_effort / thought_level | 未暴露参数 | 待实现 |
| 上下文预算 | 没有桥接参数 | 本轮新增 300k/600k/1m 验证 | 本项目新增能力 |
| 四种提示词身份与路由 | online-search、S1、S2、S3 | 通用委派与单一 Skill | 未移植；上游身份不等于 GUI 专家 |
| 旧审查会话及 SHA 绑定 | 受限 session/load 与复审锁 | 每任务新会话 | 未实现 |
| 桌面历史登记 | 原生等待，失败时直接 SQLite 写入 | 只写自有记录 | 未移植；需独立评估数据库维护风险 |
| 并行任务 | 多线程/独立运行时 | 串行单任务 | 未实现；上游宣称4并发但没有硬上限控制 |
| 任务列表 | workbuddy_list，本进程内 | 单个任务 ID 查询 | 缺列表入口 |
| 活动日志 | 分类/压缩 JSONL | 新增有限工具名、完成证据和用量字段 | 没有同等分类日志及诊断导出 |
| 可配置总期限 | 有 task timeout | wait限60秒，非任务总期限 | 缺用户可设置的任务 deadline |
| 权限 | fullAccess，自动 allow_always | default，传回用户明确选择 | 有意保留当前权限语义 |
| 任务持久化及幂等 | TASKS内存，复审绑定另存 | 自有记录、uncertain、恢复不重发、request_id、清理待确认状态 | 本项目增量 |
| 文件/部分结果 | 正文结果 | 正文路径、变化文件、部分结果 | 本项目增量 |
| 桌面 MCP/Skills可靠继承 | 无完整清单及实测证据 | Skill实测；目标MCP未通 | 双方不能凭user目录复用宣称完整继承 |
| 安装/卸载/CI | 手工配置 | 检查、备份、合并、卸载、双平台CI | Intel运行时尚未打包，依赖可能需编译 |

依据：[上游调度与工具](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/server.py)、[ACP与模型](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/acp.py)、[历史登记](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/history.py)、[复审会话](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/review_sessions.py)、[路由Skill](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/.codex/skills/workbuddy-agent-routing/SKILL.md)。

正式 v0.1.0 不要求机械复制所有上游能力，但必须完成并准确说明本项目的发布范围和验收门槛。当前不能声明三步全部完成或与上游能力完全等价。

alpha.2 历史自动测试：115 项项目测试 + 34 项上游回归，共 149 项全部通过。包含模型/上下文回显、上限拒绝、幂等参数、初始化持锁、工具结果脱敏与原生文本包装回归。

## 首次整应用退出真实结果（17:11 历史失败）

北京时间17:11:04–17:12:56运行退出恢复脚本。正常退出后旧GUI与sidecar消失；已接受的DeepSeek任务进入uncertain。重开成功，用户确认已登录的正常主界面，但100秒内未出现可认证的sidecar/CLI Host，整体验收失败。新短任务因恢复前提不成立而未提交。

后续独立MCP进程用相同request_id再次请求原任务，返回同一任务、创建/开始时间及worker，记录总数仍为1；没有新模型请求。旧worker进程已消失。脚本cleanup中的cleaned=false对应旧sidecar已经不在，不能解读为旧worker仍运行。

源码与启动日志确认sidecar/legacy host按需初始化，GUI登录正常不保证这些端点存在；当时 alpha.2 桥接器只有发现能力，没有冷启动激活能力。本次并非已验证的登录失效，也不是把等待时间延长即可确认恢复。#2/#3 当时保持开放，alpha.2 增加此兼容限制。该冷启动缺口已由本轮 alpha.3 修复并通过真实主流程复验；正式 v0.1.0 仍待上述剩余验收。本次明细无记录，积分未知。
