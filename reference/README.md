# 上游参考

来源：[gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge)。

固定提交：[c00a8e5](https://github.com/gosick233-cloud/Codex-WorkBuddy-Desktop-Bridge/tree/c00a8e5eab809a97963277e71ec008abb3d372ae)。许可证：MIT，Copyright (c) 2026 gosick233-cloud。

`upstream/` 是该提交的 README、LICENSE、requirements 和 Python 包源码，用于研究及回归。每个文件哈希记录在 `upstream-sha256.json`，运行器会验证。未包含上游运行目录、用户数据或依赖环境。

本项目自己的修改保存在 `patches/`；测试时在临时副本应用，不改写固定参考文件。`0001-macos-review-paths.patch` 修复 Mac 路径别名与文件名边界判断。

上游中 Windows 启动方式、默认全权限和数据库写入行为不是本项目的部署方案。macOS 适配必须按 [计划](../docs/PLAN.md) 单独实现。
