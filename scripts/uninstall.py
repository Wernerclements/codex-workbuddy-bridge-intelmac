#!/usr/bin/env python3
"""Remove our exact managed config block, retaining environments and results."""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

try:
    from .install import InstallError, default_config, read_config, remove_config, write_config
except ImportError:
    from install import InstallError, default_config, read_config, remove_config, write_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="卸载桥接器 MCP 配置；保留任务结果、环境和 WorkBuddy。")
    parser.add_argument("--config", type=Path, default=default_config())
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = args.config.expanduser().absolute()
        before = read_config(config)
        after = remove_config(before)
        if args.dry_run:
            print("预览完成：" + ("将删除自有 MCP 配置块。" if before != after else "没有需要删除的自有配置。"))
            return 0
        backup = write_config(config, before, after)
        print("卸载完成：自有 MCP 配置已移除；任务结果、Python 环境和 WorkBuddy 均保留。请重新加载 Codex MCP。")
        if backup:
            print(f"卸载前配置备份：{backup}")
        return 0
    except InstallError as error:
        print(f"卸载未完成：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
