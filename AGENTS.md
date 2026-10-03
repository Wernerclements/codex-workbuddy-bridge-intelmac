# 开发约定

- 项目名使用 `codex-workbuddy-bridge-intelmac`，面向 Intel Mac。
- 当前为测试基线和移植规划；不要将模拟测试通过写成真实桥接成功。
- `reference/upstream/` 固定不改。上游兼容修复保存在 `patches/` 并添加必要回归测试。
- 应用内部代码只在本机研究，不提交应用解包内容、凭证、会话正文或个人数据。
- 诊断保持只读；真实任务在独立目录进行，权限请求必须保留用户交互。
- 开发分支使用 `codex/` 前缀。
- 修改后运行 `python scripts/test_upstream.py`，更新测试报告与版本表。
- 文档优先中文，名称和说明保持简洁。
