# 开发约定

- 项目名使用 `codex-workbuddy-bridge-intelmac`，面向 Intel Mac。
- 当前源码 v0.1.2，支持列表含历史 5.6.2、5.7.7 与本机 5.8.2。5.8.2 七项核心实测、三项故障实测和本机 259 项回归通过；执行中与权限等待时整 GUI 退出恢复通过，Intel/Ubuntu 云端同样 259 项通过；发布以 Release 为准。本轮费用未核对，Codex 原生 0.1.2 待重启确认。详见 docs/WORKBUDDY_5_8_2.md。
- 2026-10-05 当前对话原生 health 确认 0.1.0。三模型真实 MCP：Deepseek/GLM 的 scansci-pdf 健康检查成功返回；三者 sheetagent 解析遇到本机编辑服务拒绝连接。Kimi 默认文档请求执行前停止；目标三连接器未发现。新增用量首四条 21.65 积分、后续三条未知。首次 Deepseek 取消清理记录仍 pending，独立自有恢复已确认物理清理；不得把它称为自动同步。详见 docs/MCP_TEST.md。
- health 保持只读；ready_for_task 与 runtime_ready、authenticated 分开报告，冷启动 authenticated=null 不代表认证失败。
- 原生保护材料仅在应用内置运行时和临时私有 socket 握手中使用，不流入 Python、日志、任务记录或文档。保留字段加密，应用密钥辅助函数 createIfMissing=false；产品缓存只读投影，未知版本拒绝任务。
- 只清理自有 worker 与进程组，保留权限交互、request_id 幂等、uncertain 与 cleanup_pending/cleanup_confirmed 语义；GUI 退出不能误报成功。
- `reference/upstream/` 固定不改。上游兼容修复保存在 `patches/` 并添加必要回归测试。
- 应用内部代码只在本机研究，不提交应用解包内容、凭证、会话正文或个人数据。
- 诊断保持只读；真实任务在独立目录进行，权限请求必须保留用户交互。
- 开发分支使用 `codex/` 前缀。
- 修改后运行 `python scripts/test_upstream.py` 和 `python scripts/test_bridge.py`，更新测试报告与版本表。
- 文档优先中文，名称和说明保持简洁。
