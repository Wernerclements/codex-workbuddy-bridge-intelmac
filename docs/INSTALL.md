# 安装与卸载

项目：`codex-workbuddy-bridge-intelmac`。本地安装的是 Codex 使用的 MCP 服务，无须额外打开桌面窗口。WorkBuddy 应保持已安装、主界面运行且已登录。当前说明对应 v0.1.2，实际发布状态以仓库 Release 为准。

## 环境要求

- 原生 Intel Mac（`x86_64`），不以 Rosetta 环境代替 Intel 验收。
- 已有 Intel Python **3.12 或以上**；优先使用 3.12，不自动下载大型运行时。
- `/Applications/WorkBuddy.app`，适配 **5.6.2、5.7.7、5.8.2**（5.7.6 未完成验收）。安装前先核对仓库的兼容表和真实测试报告。
- 已安装 Codex.app；安装器默认使用 `CODEX_HOME/config.toml`，未设置时使用 `~/.codex/config.toml`。

安装器检查平台、Python 架构、应用版本和应用的 Intel 可执行文件。版本检查只表示符合适配基线，真实任务能力以测试报告为准。未知 WorkBuddy 版本默认拒绝安装；开发者完成诊断后可显式使用 `--allow-unverified-version`，该选项不代表版本已获得支持。

## 1. 预览

在项目源码目录执行：

```sh
python3 scripts/install.py --dry-run
```

预览检查配置冲突，不安装依赖、不创建环境、不修改 Codex 配置，也不输出已有配置的值。

需要指定已有解释器或独立环境时：

```sh
python3 scripts/install.py --dry-run \
  --python /usr/local/bin/python3.12 \
  --venv "$HOME/.local/share/codex-workbuddy-bridge-intelmac/venv"
```

所有路径参数都支持包含空格的路径；在终端中用引号包住它们。

WorkBuddy 不在默认位置时，使用 `--app "/自定义路径/WorkBuddy.app"`。安装器会检查该应用，并在自有 MCP 表中记录 `env = { WORKBUDDY_APP = "绝对路径" }`，保证实际运行连接同一应用。再次安装须使用相同应用路径；更改路径时先卸载原配置。默认应用位置无需额外环境变量。

## 2. 安装

```sh
python3 scripts/install.py
```

安装器默认在项目目录创建 `.venv`，安装固定依赖和源码，再追加唯一的 MCP 服务 `workbuddy_intelmac`。**源码采用可编辑安装；项目目录应保留，移动目录后需卸载原配置并重新安装。** 可用 `--venv` 指定环境位置，但环境仍引用这个源码目录。

第一次依赖安装需要网络。默认要求原生依赖使用预编译包；Intel 平台部分新版本没有这样的包，安装会明确失败，此时 Codex 配置仍未修改。本项目基线测试中 `cryptography 50.0.2` 曾需要源码编译。

如果已经安装 Apple 开发工具、Rust 和 `pkg-config`，并愿意承担本机编译，可以使用：

```sh
python3 scripts/install.py --allow-source-build
```

该选项只允许依赖安装从源码构建，不代为安装编译工具。编译还可能需要兼容的 OpenSSL 开发文件；现有工具链是否能完成编译，以实际依赖安装结果为准。安装失败可以在修复环境后重复执行，已创建的自有环境会保留。

**v0.1.2 冷启动：** 不要求 sidecar/CLI Host 事先运行。已登录 GUI 的唯一实例存在时，实际任务可创建自己的应用内置运行时。原生受保护字段握手与产品配置缓存必须可用；未知应用版本、缺失保护材料或不安全的缓存文件会明确失败。无需另配模型 API 或手工导出登录凭证。设计和维护说明见 [冷启动设计](COLD_START.md)。

安装完成后，在 Codex 中重新加载 MCP 服务；若当前版本没有独立的重新加载入口，重启 Codex.app。先调用桥接器的 `health` 查看状态，再在独立目录执行一个小型验收任务。

只读 health 不创建 worker：没有 sidecar 时，`ready_for_task=true`、`runtime_ready=false`、`authenticated=null` 表示具备启动条件、运行时尚未启动、认证尚未验证，可继续启动任务；`available=false` 时先按错误处理。实际任务成功后，再核对 WorkBuddy 账号与新用量。更新源码后已有 MCP 进程仍可能使用旧代码，须重新加载或完全重启 Codex；旧版原生 health 通过不替代这一步。

## 配置如何处理

- 默认配置：`~/.codex/config.toml`，也可通过 `--config` 指定。
- 只追加 `[mcp_servers.workbuddy_intelmac]`，使用标准输入输出启动 `python -m workbuddy_bridge_intelmac.server`。
- 原配置按字节保留，不重排、不输出其他配置值；只检查表名和键名以发现冲突。
- 写入前保留一份权限为 `0600` 的备份，名称类似 `config.toml.workbuddy-intelmac-日期-随机标记.bak`。
- 同名用户配置、另一个安装路径、手工修改过的自有配置块、符号链接或写入期间的配置变化都会中止操作。
- 重复安装同一路径不会追加第二个服务。依赖检查仍会执行。
- 安装器不会启动、停止或修改 WorkBuddy 会话，也不访问应用登录凭证。

需要定制超时等配置时，先保存改动。安装器检测到自有配置块内容变化后会拒绝自动覆盖或删除，需要人工核对；这能避免升级或卸载丢失用户修改。

## 可选 Skill

Skill 位于 `skills/workbuddy-bridge/SKILL.md`，只提供委派与结果读取约定。MCP 服务连接成功后，可将整个 `workbuddy-bridge` 目录放入 Codex 的用户 Skill 目录（默认 `~/.codex/skills/`），再重新加载。

Skill 安装不是运行 MCP 的必要条件。安装器不会覆盖任何已有 Skill；如同名目录已经存在，先人工核对。

## 卸载

先预览，再移除本安装器创建的配置块：

```sh
python3 scripts/uninstall.py --dry-run
python3 scripts/uninstall.py
```

若安装时指定了 `--config`，卸载时使用同一个路径。

卸载不会删除 Python 环境、项目源码、Skill、任务记录或结果文件，也不会停止 WorkBuddy。重新加载 Codex MCP 后，服务不再被启动。已有任务请先查询并正常结束；移除配置本身不等于取消任务。

手工清理可选 Skill 和 Python 环境前，应确认桥接器已停止且结果已保留。不要直接删除仍有任务运行的项目目录。

## 回滚与临时测试

安装和卸载都会输出备份路径。优先使用卸载移除自有配置，再保留其它服务的后续改动。只有确认配置自备份以来没有需要保留的新修改时，才用完整备份恢复原配置。

在临时配置路径验证安装逻辑：

```sh
python3 scripts/install.py --dry-run --config /tmp/workbuddy-intelmac-config.toml
python3 -m unittest discover -s tests -p 'test_install.py' -v
```

测试会使用临时目录，覆盖配置保留、冲突、备份、重复安装、卸载、符号链接和写入期间变化，不访问真实用户配置。
