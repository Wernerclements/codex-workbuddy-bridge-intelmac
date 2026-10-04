# 开发约定

- 项目名使用 `codex-workbuddy-bridge-intelmac`，面向 Intel Mac。
- 当前为 alpha.3 发布候选；冷启动、权限、取消、故障和整 GUI 退出/重开真实矩阵通过。最终自动测试与仓库 Release 决定实际发布状态，不提前声称已发布；正式版仍待 Codex 原生加载更新代码、新用量核对与发布检查。旧暖启动用量已确认，新路径用量仍待确认。
- health 保持只读；ready_for_task 与 runtime_ready、authenticated 分开报告，冷启动 authenticated=null 不代表认证失败。
- 原生保护材料仅在应用内置运行时和临时私有 socket 握手中使用，不流入 Python、日志、任务记录或文档。保留字段加密，应用密钥辅助函数 createIfMissing=false；产品缓存只读投影，未知版本拒绝任务。
- 只清理自有 worker 与进程组，保留权限交互、request_id 幂等、uncertain 与 cleanup_pending/cleanup_confirmed 语义；GUI 退出不能误报成功。
- `reference/upstream/` 固定不改。上游兼容修复保存在 `patches/` 并添加必要回归测试。
- 应用内部代码只在本机研究，不提交应用解包内容、凭证、会话正文或个人数据。
- 诊断保持只读；真实任务在独立目录进行，权限请求必须保留用户交互。
- 开发分支使用 `codex/` 前缀。
- 修改后运行 `python scripts/test_upstream.py` 和 `python scripts/test_bridge.py`，更新测试报告与版本表。
- 文档优先中文，名称和说明保持简洁。
