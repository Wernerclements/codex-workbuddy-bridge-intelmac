# 开发约定

- 项目名使用 `codex-workbuddy-bridge-intelmac`，面向 Intel Mac。
- 当前为 alpha 开发版；整应用冷启动恢复未通过，不能正式发布。区分真实验收、模拟测试和新增用量待确认项；双主力模型订阅已确认。
- `reference/upstream/` 固定不改。上游兼容修复保存在 `patches/` 并添加必要回归测试。
- 应用内部代码只在本机研究，不提交应用解包内容、凭证、会话正文或个人数据。
- 诊断保持只读；真实任务在独立目录进行，权限请求必须保留用户交互。
- 开发分支使用 `codex/` 前缀。
- 修改后运行 `python scripts/test_upstream.py` 和 `python scripts/test_bridge.py`，更新测试报告与版本表。
- 文档优先中文，名称和说明保持简洁。
