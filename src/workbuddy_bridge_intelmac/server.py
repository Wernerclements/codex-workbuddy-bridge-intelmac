"""Codex 的 MCP stdio 入口。等待工具不阻塞取消或权限响应。"""
from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP

from .manager import Manager


def create_server(manager: Manager | None = None):
    manager_lock = threading.Lock()

    def get_manager():
        nonlocal manager
        with manager_lock:
            if manager is None:
                try:
                    manager = Manager()
                except RuntimeError as error:
                    if "已有桥接服务使用此任务目录" in str(error):
                        raise RuntimeError(
                            "另一个 Codex 对话正在使用 WorkBuddy 任务服务；"
                            "本对话仍可检查 health。请在原对话操作任务，"
                            "或关闭原服务后重试。当前版本只允许一个任务服务持有记录目录。"
                        ) from None
                    raise
            return manager

    async def call_manager(method, *arguments):
        def invoke():
            return getattr(get_manager(), method)(*arguments)
        return await asyncio.to_thread(invoke)

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {"get_manager": get_manager}
        finally:
            if manager is not None:
                await asyncio.to_thread(manager.close)

    server = FastMCP("WorkBuddy Intel Mac Bridge", lifespan=lifespan, log_level="WARNING")

    @server.tool()
    async def start_task(prompt: str, cwd: str, request_id: str | None = None,
                         model: str = "deepseek-v4.1-flash",
                         context_tokens: int | None = None) -> dict:
        """排队一个新任务。建议提供 request_id；响应丢失时原样重试该标识，避免重复执行。cwd 必须是现有目录。"""
        return await call_manager("start_task", prompt, cwd, request_id, model, context_tokens)

    @server.tool()
    async def get_task(task_id: str) -> dict:
        """查询任务状态；permission_required 时请向用户展示权限请求。"""
        return await call_manager("get_task", task_id)

    @server.tool()
    async def wait_task(task_id: str, timeout_seconds: float = 30) -> dict:
        """等待最多 60 秒；等待超时不会取消、重新提交任务。"""
        return await call_manager("wait_task", task_id, timeout_seconds)

    @server.tool()
    async def cancel_task(task_id: str) -> dict:
        """取消排队或执行中的任务；不会停止其他 WorkBuddy 会话。"""
        return await call_manager("cancel_task", task_id)

    @server.tool()
    async def get_result(task_id: str) -> dict:
        """读取终态结果摘要、完整正文位置和工作目录内变化文件的清单。"""
        return await call_manager("get_result", task_id)

    @server.tool()
    async def health() -> dict:
        """只读检查 WorkBuddy 安装、运行实例和协议能力。"""
        from .adapter import health as check
        return await asyncio.to_thread(check)

    @server.tool()
    async def respond_permission(task_id: str, request_id: str, option_id: str) -> dict:
        """用户明确选择后，提交当前权限请求中的 optionId；不自动允许权限。"""
        return await call_manager("respond_permission", task_id, request_id, option_id)

    return server


def main():
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
