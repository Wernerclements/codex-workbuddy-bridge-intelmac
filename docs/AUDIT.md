# 验收状态与上游差距

核对日期：2026-10-04（北京时间）。版本：`v0.1.0-alpha.2`。正式 `v0.1.0` 尚未发布。

## 三项待办的严格状态

| 待办 | 当前状态 | 已完成 | 剩余 |
| --- | --- | --- | --- |
| #1 单任务通道 | 已关闭 | Intel Mac 发现、独立运行时、真实文本及文件往返 | 原生桌面专家和任意旧会话不属于已实现能力 |
| #2 生命周期验收 | 重新开放 | 取消、允许一次/拒绝、错误凭证、worker 失联、桥接重启不重发 | 原计划的整个 WorkBuddy 应用退出与重启真实验收 |
| #3 安装与发布 | 开放 | 本机安装、配置保留及备份、Skill、alpha 发布、指定模型账号用量核对 | 修复后 Codex 界面重新加载的实际调用；正式发布 |

此前将 #2 关闭不代表原计划全部验收完成。重新开放使待办状态与证据一致；不会把模拟结果或自有 worker 失联当作整个桌面退出实测。

## 两个主力模型与上下文

真实任务通过 MCP stdio 提交，先核对模型目录、选择并确认指定模型，再核对上下文选项。没有配置独立模型 API。

| 模型 | 模型 ID | 默认预算 | 可选预算 | 本次确认 |
| --- | --- | --- | --- | --- |
| Deepseek-V4.1-Flash | deepseek-v4.1-flash | 300,000 | 300k / 600k / 1m | 任务成功；usage_update.size=1,000,000 |
| GLM-5.3-Flash | glm-5.3-flash | 300,000 | 300k / 600k / 1m | 任务成功；usage_update.size=1,000,000 |

`start_task` 新增 `model` 与 `context_tokens`。默认模型为 Deepseek-V4.1-Flash；未指定上下文时使用应用该模型的默认预算，长资料任务可指定 `context_tokens=1000000`。同一 request_id 的模型或上下文改变会被拒绝，不能静默重发。

上下文设置是 session 内存覆盖，模型参数在 worker 内使用；不修改桌面全局默认模型。自动压缩窗口与上下文预算分别处理，1m 设置不是关闭压缩或 Max 模式。单次 prompt 上限仍为 100000 字符；本次短任务只验证设置与运行时预算，没有测试接近百万 token 的长输入承载或质量。

用户于 2026-10-04 提供 WorkBuddy 用量页记录并确认消耗正常：北京时间 16:07、16:10、16:20 的 Deepseek-V4.1-Flash 分别扣减 0.60、0.61、0.51 积分，共 1.72；16:08 的 GLM-5.3-Flash 扣减 0.54 积分，合计 2.26。时间和模型与本次四项真实测试对应，使用端显示 CLI。结合此前账号归属与无额外计费提示的确认，两个主力模型的订阅复用验收通过。

## MCP 与 Skill 继承

- 两个模型都实际调用了 ToolSearch 和 Skill；Skill 加载现有 find-skills。工具调用事件与完成状态可核对。
- 补充读取本次测试任务自身的原生 CLI 历史：Skill 结果的 Base directory 精确指向 ~/.workbuddy/skills/find-skills，继承有原生结果证据；ToolSearch 返回1个相关候选 mcp__sheetagent__search_data，四个目标工具均不匹配。不会把任意 found 状态当作目标工具已发现。
- 原生 Skill 工具返回渲染后的正文，不含 YAML frontmatter；文中版本表 v1.7 与原文件 frontmatter 1.7.0 的表现不同是正常渲染行为。
- 默认桥接会话没有发现通达信、同花顺和目标法律工具。
- 桌面当前连接器启用状态包含这些服务。桥接目前没有采用桌面按会话生成 MCP 配置的投影步骤。
- 实验性地只为测试 worker 显式加载现有 mcp.json 后，仍未发现目标工具；不能简单通过传配置文件补齐。
- 没有调用交易、修改自选、发消息或其他第三方业务操作。本次工具发现不能当作第三方调用验收通过。

当前版本进一步验证了 WBIPC 的本机认证握手和只读协议清单：protocol=1，唯一 pipe 为 wb.request，唯一方法为 http.fetch。该通道是认证云端请求转发，不暴露 mcpList 或会话 MCP 配置投影。没有发送业务请求、提交模型任务或读取第三方数据。桌面会话的工具投影还需要独立适配。

缺口单独记录在 [#5 继承 WorkBuddy 桌面连接器](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/issues/5)。历史缓存和界面启用状态不能替代桥接当前会话的真实工具发现与只读调用。后续步骤见 [桌面连接器适配](CONNECTORS.md)。

## Codex 多对话初始化问题

旧实现启动即创建 Manager 并独占全局任务目录；第一个对话启动成功后，其他对话握手关闭。现改为首次任务操作时创建 Manager，初始化、工具发现与 health 不再拿任务锁。

已在原任务服务持锁时，实际启动两个 stdio 客户端，均完成初始化、7工具发现与 health。Codex 界面重新加载后还需要确认。2026-10-04 本对话仍未暴露 workbuddy_intelmac 工具；应用内部重载接口只经现有受信任连接调用，当前没有可访问的控制 socket 或重载工具。用户确认设置没有重载/开关入口，因此需在初始化修复后再次完全退出并打开 Codex.app，再执行一次原生 health。尚未用独立 stdio 检查替代这项验收。任务操作仍由单个服务持有记录目录；未实现跨对话共享后台调度器，遇到冲突明确报告，不能将初始化修复称为多任务并发已实现。

## 与上游逐项对照

上游最新 main 与本项目固定基准相同：`c00a8e5eab809a97963277e71ec008abb3d372ae`。没有遗漏后续上游提交。

| 能力 | 上游 | Intel Mac 当前版 | 差距/选择 |
| --- | --- | --- | --- |
| 单任务启动、等待、查询、取消 | 有 | 有，真实验收 | 核心通道覆盖，工具名称不同 |
| 执行通道 | 应用内置 CLI Host + sidecar + ACP | 相同架构，完成 Intel Mac 适配 | 两者都不是任意 GUI 专家直接控制 |
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
| 任务持久化及幂等 | TASKS内存，复审绑定另存 | 自有记录、uncertain、恢复不重发、request_id | 本项目增量 |
| 文件/部分结果 | 正文结果 | 正文路径、变化文件、部分结果 | 本项目增量 |
| 桌面 MCP/Skills可靠继承 | 无完整清单及实测证据 | Skill实测；目标MCP未通 | 双方不能凭user目录复用宣称完整继承 |
| 安装/卸载/CI | 手工配置 | 检查、备份、合并、卸载、双平台CI | Intel运行时尚未打包，依赖可能需编译 |

依据：[上游调度与工具](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/server.py)、[ACP与模型](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/acp.py)、[历史登记](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/history.py)、[复审会话](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/workbuddy_bridge/review_sessions.py)、[路由Skill](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/blob/c00a8e5eab809a97963277e71ec008abb3d372ae/.codex/skills/workbuddy-agent-routing/SKILL.md)。

正式 v0.1.0 不要求机械复制所有上游能力，但必须完成并准确说明本项目的发布范围和验收门槛。当前不能声明三步全部完成或与上游能力完全等价。

本机最终自动测试：115 项项目测试 + 34 项上游回归，共 149 项全部通过。包含模型/上下文回显、上限拒绝、幂等参数、初始化持锁、工具结果脱敏与原生文本包装回归。
