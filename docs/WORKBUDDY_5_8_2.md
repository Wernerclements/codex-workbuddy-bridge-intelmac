# WorkBuddy 5.8.2 适配记录

记录日期：2026-10-15。对象：本机 Intel Mac、WorkBuddy **5.8.2**、桥接 **0.1.2**。**状态：七项核心、三项故障、两种整 GUI 退出恢复均通过。** 本机与 Intel macOS / Ubuntu 云端各通过全部 259 项；正式发布状态见 Release。

依据为本机忽略目录中的 `.local/update-582/protocol-audit.md` 和 `live-report.json`。本报告仅归纳接口与脱敏验收事实，不包含应用解包代码、凭证、原始保护材料或用户会话正文。

## 原生保护模块与精确版本边界

5.8.2 的保护 helper 从旧 `main/process-cpu-sampler.js` 迁移至 **`main/app-lifetime-job.js`**，六个导出全部变化：

| 桥接功能 | 5.8.2 导出 |
| --- | --- |
| `init_dist` | `T` |
| `normalizeAtRestBuildKeyCapabilities` | `U` |
| `keyblobPathFor` | `D` |
| `loadOrCreateKeyblobMasterKey` | `O` |
| `encodeAtRestEncryptionBootstrap` | `C` |
| `disposeAtRestEncryptionBootstrap` | `S` |

launcher 按精确版本选择模块路径和全部导出，并确认它们均为函数；不能只修改旧模块的短名。保留应用原生 storage binding、`createIfMissing=false`、`fields` 加密、私有 credential/ready socket 身份校验和 Buffer 清零。保护材料只在应用内置运行时处理，不进入 Python 或任务记录。sidecar 协议仍为 **6**，CLI 入口和 loopback ACP 路径保留既有契约。

支持列表保留历史 **5.6.2、5.7.7**，增加 **5.8.2**。旧版映射独立保留；本轮未降级重跑其桌面验收。未知版本仍默认拒绝任务，Intel 和非 Rosetta 检查继续生效。

## 认证类型、缓存与模型就绪

产品认证保持 `authentication.id=workbuddy-desktop`，类型严格绑定应用版本：

| WorkBuddy 版本 | 允许的产品认证类型 |
| --- | --- |
| 5.6.2、5.7.7 | `cli-external-link` |
| 5.8.2 | `account-scoped-external-link` |

保留 GUI snapshot 中的完整认证属性与 endpoint，不将新类型改写成旧类型，也不提取、复制或转换账号材料。本版未发现桥接必须新增的账号或 token 环境变量；账号恢复仍由应用 provider 完成。认证类型匹配本身不等于真实任务或订阅计费验证成功。

5.8.2 的 `CODEBUDDY_DISABLE_PRODUCT_CACHE` 会同时跳过 `ProductManager.loadFromCache()` 的读取和 `saveToCache()` 的写入。这个结论只针对上述两处产品缓存，不能扩大为整个 CLI 不写日志、会话或其它缓存。桥接继续在应用进程中只读投影 GUI 产品配置，Python 不读取缓存正文，也不删除或改写缓存。

**模型就绪门保持。** 指定模型必须同时出现在产品 `models` 和 `cli` agent 模型列表中，才冻结本次投影并加载 CLI。新版增加了 snapshot 修复逻辑，但 runtime 尚未 ready 时仍可能返回旧目录，不能因此删除这个等待门。实际模型选择与上下文预算继续以本次会话广告和选择回显验证，不替换模型或自动重发 prompt。

## 应用预热 CLI 与 GUI 身份

本机发现应用自己的预热 CLI：Electron 后带 Node 优化参数，再进入 CLI entry，并包含 `--prewarm`、`--prewarm-id`。旧识别逻辑可能将其误计为第二个 GUI，阻止自有运行时启动。

GUI 身份识别已排除 `--prewarm`。这些预热进程属于 WorkBuddy，不是桥接自有 worker；本轮未向它们发送信号或执行清理。清理仍须校验桥接自有身份，不能以进程使用同一 Electron 可执行文件作为归属依据。

## 当前真实验收

下表依据 `result`、`state` 的终态及清理字段。各测试初次提交记录中的 **`queued` 只是提交状态，不能作为最终结果**。

| 项目 | 当前证据 | 验收范围 |
| --- | --- | --- |
| Deepseek-V4.1-Flash | `succeeded / end_turn`，返回 `WB582_DEEPSEEK_OK` | 短文本、指定模型选择与自有运行时清理 |
| GLM-5.3-Flash | `succeeded / end_turn`，返回 `WB582_GLM_OK` | 短文本、指定模型选择与自有运行时清理 |
| Step-5-Preview | `succeeded / end_turn`，返回 `WB582_STEP_OK` | 短文本；会话选择确认 **1,000,000 tokens**，未测试接近 1m 的实际输入 |
| Write 文件 | 明确 `allow_once`，终态 `succeeded`；清单包含 `result.txt` | 验收文件内容为 `WB582_FILE_OK`，实物与清单核对通过 |
| Bash 文件 | 明确 `allow_once`，终态 `succeeded`；清单包含 `result.txt` | 命令执行完成，内容为 `WB582_BASH_OK` |
| 权限拒绝 | 明确 `reject_once`，终态 `cancelled`，无结果文件 | 拒绝未被自动替换为允许；本轮记录 `passed=true` |
| 流式取消 | 已开始正文输出后取消，终态 `cancelled`，`partial=true` | 保留部分结果，自有运行时清理确认；未将部分正文当作完整完成 |

上表七项均记录 `cleanup_confirmed=true`、`cleanup_pending=false`。前六项 `partial=false`；流式取消为 `partial=true`，记录 `stream_started=true`。模型短任务和文件结果成功证明这些请求完成；费用与 Codex 当前原生服务的版本仍需分别核对。

适配后，真实矩阵开始时的只读 health 显示 `ready_for_task=true`、`runtime_ready=false`、`authenticated=null`，执行模式为 `owned_cold_start`。这表示允许尝试自有冷启动，不将空闲时尚未启动运行时解释为认证失败，也不把健康检查当作已经完成模型请求。

## 故障与自动回归

本轮额外完成三项真实故障验收：错误本机认证被拒绝；桥接自有 worker 失联后原任务进入 `uncertain` 并清理确认；本轮自有 MCP 服务被强制结束、重开后原任务 `uncertain`、自有资源恢复确认。两类中断期间 WorkBuddy GUI 保持可用；未向 GUI 或原生预热进程发送强制终止信号。证据保存在忽略目录 `.local/update-582/faults/report.json`。

本机自动测试 **259 项通过：225 项项目测试 + 34 项上游回归，无跳过**。[Intel macOS / Ubuntu 云端检查](https://github.com/Wernerclements/codex-workbuddy-bridge-intelmac/actions/runs/38728339436) 对相同实现各执行 259 项，也全部通过。新增覆盖 5.8.2 安装/备份、新模块路径及短名、模块或导出缺失拒绝、认证类型与版本不匹配拒绝；扩展版本元数据、预热进程排除、模型就绪与超时门槛回归。旧版本为历史基线，没有降级重跑桌面实测。

云端文档提交曾出现一次旧测试竞态：后台清理已被安排但尚未交接实例锁，重启测试立即创建新服务，被正确拒绝。已让正常重启测试等待受保护的锁交接；生产清理与实例锁规则未改，专门验证“恢复未结束时继续持锁”的用例保留。管理器 40 项及相关重启用例额外 50 轮通过，完整项目仍为 225 项，不将重复运行累计为新增覆盖。最终检查见 PR #17；初次失败记录保存在本机，未抹去。

## 整应用退出恢复

用户确认应用空闲后，于 2026-10-15 北京时间 **19:56:56–19:58:55** 完成执行中退出测试，**19:59:37–20:01:10** 完成权限等待退出测试，两轮 `passed=true`。

两轮均正常退出整个 WorkBuddy，原任务进入 `uncertain`、权限等待解除、相同请求返回原记录且不重发。重开后先只读检查启动条件，再由新的自有运行时完成预期短任务，以实际模型响应确认认证恢复。每轮两个 worker 均确认清理，权限轮次的测试文件未生成。未向 GUI 或其它非自有进程发送强制终止信号，结束时应用已重新打开。

证据分别保存在本机忽略目录的 `restart-stream.log`、`restart-permission.log` 和本次带时间戳的独立任务记录；报告不上传原始会话。

## 剩余边界

- 本次流式取消为 Deepseek，不代替其他模型的单独取消验收。没有重跑 MCP 业务调用；#5 桌面连接器与 #11 延迟工具权限详情仍开放。
- 本轮费用未核对，不据模型标签、登录恢复或任务成功推断实际扣费。
- 当前 Codex 对话已运行的原生 MCP 服务可能仍为旧版本。**0.1.2 需完全重启 Codex 后，再只读核对 `bridge_version`**；独立 stdio 验收不能替代这一步。
- 2026-10-15 一次当前对话原生 health 已识别 5.8.2 且 ready_for_task=true，但仍报告 bridge_version=0.1.1。独立新服务确认 0.1.2；当前对话还需完全重启后只读确认新版。
- 云端结果与正式发布状态以对应提交检查与 Release 为准。
