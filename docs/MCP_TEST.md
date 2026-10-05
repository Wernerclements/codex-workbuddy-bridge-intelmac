# 三模型 MCP 实测

日期：2026-10-05（北京时间）。环境：2019 Intel Mac、macOS 26.5.1、WorkBuddy 5.6.2、桥接器 v0.1.0。通过 Codex 当前对话的原生 MCP 入口提交；未修改生产执行代码。

## 结论

**Codex 委派的 WorkBuddy 新任务能够实际调用部分现有 MCP。** Deepseek-V4.1-Flash 和 GLM-5.3-Flash 均通过 `DeferExecuteTool` 调用 `mcp__scansci-pdf__scansci_pdf_health_check`，参数为 `detailed=false`，返回可核对的服务状态。不是仅凭 ToolSearch 候选或模型自述验收。

三个模型均实际执行过 `mcp__sheetagent__resolve_local_excel`，但表格服务的本机编辑器接口拒绝连接，未取得表格数据。通达信、同花顺和目标法律连接器仍未在本轮定向搜索中发现，不能扩大为完整继承所有桌面 MCP。

## 任务与提示词

先用相同定向提示词要求三个模型发现目标连接器，找到后实际完成一次最小只读查询。用户随后将目标扩大为任意已连接 MCP，追加对应的新任务。

扩大范围后的三个模型使用同一份随机生成的虚构 Excel，分别放在各自独立目录；预期数值和随机标记不放入提示词，也不放在任务工作目录中。优先要求通过 MCP 解析并读取文件；失败后允许其他已连接 MCP 的无副作用状态查询。禁止通过普通文件读取、命令或网页替代 MCP。

Deepseek 首次扩大范围的任务尝试目录搜索，随后请求列目录。用户拒绝一次后，WorkBuddy 返回 cancelled，任务没有继续；因此增加一次采用更明确约束的 Deepseek 任务。GLM、Kimi 与这次 Deepseek 任务均禁止目录检查，要求直接使用 MCP。

所有模型回显均确认选择准确，默认会话预算为 300k。Kimi K3 的实际模型 ID 为 `kimi-k3-1`。本轮不测试百万 token 输入。

## 实际结果

| 模型 | 定向 ToolSearch 实际次数 | 三个原目标 | 表格 MCP 实际调用 | 其他 MCP 实际调用 |
| --- | ---: | --- | --- | --- |
| Deepseek-V4.1-Flash | 6 | 未发现 | resolve_local_excel 返回本机连接错误，未读取数据 | scansci-pdf 健康检查成功返回 |
| GLM-5.3-Flash | 5 | 未发现 | 同一服务连接错误，未读取数据 | scansci-pdf 健康检查成功返回 |
| Kimi K3 | 6 | 未发现 | 同一服务连接错误，未读取数据 | 未验收；后续默认文档解析请求被停止 |

GLM 自述有六次定向搜索，原生记录证明实际五次，其中一次合并同花顺和法律关键词。按原生记录计数，不采用模型自述。

本轮实际完成五次 MCP 调用：三个模型各一次表格解析、Deepseek 与 GLM 各一次服务健康检查。未授权的 Kimi 默认文档解析只有请求，未执行，不计入完成次数。ToolSearch 不计为第三方 MCP 业务调用。

### 表格解析

三个模型的请求均传入自己目录中 `bridge-probe.xlsx` 的完整绝对路径。工具完成事件与原生调用/返回按 callId 对应，返回：

```text
error_code: UNOPENABLE_LOCAL_PATH
stage: open_file
connect ECONNREFUSED 127.0.0.1:39099
```

测试文件存在，三个副本相同。错误正文定位到本机编辑器接口连接被拒，不能采用返回中的笼统 wrong_path 提示认定 Codex 传错路径。没有取得 file_id/sheet_id 或数据行，随机标记、数值及合计均未验收。不修改或启动该外部服务以掩盖本轮失败。

### scansci-pdf 健康检查

Deepseek 与 GLM 的实际调用均返回 `overall=degraded`。这表示 MCP 方法成功执行并报告部分依赖不健康，不表示所有功能正常。

- GLM 的返回：Europe PMC、Unpaywall、OpenAlex、Crossref 状态为 ok；CORE 与 Semantic Scholar 无响应；Tor 与 FlareSolverr 不可用。
- Deepseek 的返回：上述六个数据源均为 ok；Tor 与 FlareSolverr 不可用。

两次请求时间不同，网络检查状态不同不能作为模型能力差异。只做服务状态检查，没有下载论文、安装程序或更改配置。

### Kimi 的边界

Kimi 的文件解析失败后，请求省略 local_path、解析当前默认文档。用户只授权虚构文件，故在执行前取消任务，保留 partial 结果。没有访问默认文档，没有继续新增 Kimi 收费任务。不能据此判断 Kimi 无法调用健康检查，或将本轮认定为完整的三模型同工具成功率比较。

## 证据与资源

实际调用证据来自 ACP 工具活动和本轮任务自身的原生 function_call/function_call_result；按 callId 核对真实工具名、参数与返回。只读取这些测试任务自己的记录，不查询其他用户会话。原始会话、文件、任务标识和完整健康检查输出只保存在本机忽略目录 `.local/prompt-mcp-20261005`，不提交仓库。

本轮共七条 prompt 被执行通道接受；另一次 Deepseek 启动被模型目录检查拦截，未发送 prompt。之后无 prompt 的模型目录查询与三个模型的实际选择均成功；首次目录不完整的原因尚未确认，不称为已修复。

定向三任务、GLM/Kimi 扩大范围任务与最后一次 Deepseek 任务，服务均确认清理。首次扩大范围的 Deepseek 在拒绝权限后结束，任务记录仍为 cleanup_pending；独立复核证明自有进程组为空，并用已有 recover_owned 清理剩余空私有目录，返回 cleaned=true。没有重写活动服务的内存记录，不能把该项记录称为已自动同步。后续需改进延迟清理确认。

延迟工具权限目前只显示包装器 DeferExecuteTool、input={}。本轮通过对应任务自身的原生调用确认实际 MCP 和参数后，依据用户明确授权逐次选择 allow_once；未选择永久允许。权限展示需要改进。

## 已核对用量与未确认部分

用户提供 WorkBuddy 账号用量页，以下四条使用端均为 WorkBuddy：

| 北京时间 | 模型 | 积分 | 对应任务 |
| --- | --- | ---: | --- |
| 15:56 | Deepseek-V4.1-Flash | 1.11 | 原三目标定向发现 |
| 15:57 | GLM-5.3-Flash | 0.60 | 原三目标定向发现 |
| 15:58 | Kimi K3 | 18.99 | 原三目标定向发现 |
| 16:01 | Deepseek-V4.1-Flash | 0.95 | 首次扩大范围，拒绝目录查看后结束 |
| **已核对合计** | | **21.65** | 四条用量记录 |

这四条不是全部测试总费用。GLM 16:14、Kimi 16:21、Deepseek 16:23 开始的三条扩大范围任务，最终用量尚待用户核对；停止任务不能推断消耗为零。Kimi 已核对的 18.99 积分属于定向发现任务，不是成功查询费用，也不能直接推算后续任务成本。

## 后续与发布

本轮是现有 v0.1.0 的能力验收与文档/Skill 说明更新，不变更产品执行代码或发布新版本。更新后本机再次执行 34 项上游回归与 198 项项目测试，共 232 项全部通过。

#5 保持开放：部分 MCP 可调用，不等于目标桌面连接器的完整继承。延迟权限展示跟踪 [#11](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/issues/11)，延迟清理确认跟踪 [#12](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/issues/12)。表格本机服务依赖与首次模型目录异常保留本轮失败证据，未在此轮修复。
