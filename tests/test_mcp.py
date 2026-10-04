import asyncio
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from workbuddy_bridge_intelmac.manager import Manager
from workbuddy_bridge_intelmac.server import create_server
from test_manager import Controller


def structured(result):
    if isinstance(result, tuple):
        return result[1]
    if isinstance(result, dict):
        return result
    return json.loads(result[0].text)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.cwd = Path(self.temporary.name) / "work"
        self.cwd.mkdir()
        self.controller = Controller()
        self.manager = Manager(Path(self.temporary.name) / "state", self.controller.factory, lambda runtime: {})
        self.server = create_server(self.manager)

    def tearDown(self):
        self.manager.close()
        self.temporary.cleanup()

    async def test_tools_expose_permission_and_validate_inputs(self):
        tools = await self.server.list_tools()
        self.assertEqual({tool.name for tool in tools}, {"start_task", "get_task", "wait_task",
                         "cancel_task", "get_result", "health", "respond_permission"})
        permission = next(tool for tool in tools if tool.name == "respond_permission")
        self.assertEqual(set(permission.inputSchema["required"]), {"task_id", "request_id", "option_id"})
        with self.assertRaises(Exception):
            await self.server.call_tool("wait_task", {"task_id": "missing", "timeout_seconds": 61})

    async def test_wait_does_not_block_cancel_and_stdout_is_clean(self):
        captured = io.StringIO()
        with redirect_stdout(captured):
            result = structured(await self.server.call_tool("start_task", {"prompt": "test", "cwd": str(self.cwd)}))
            task_id = result["task_id"]
            await asyncio.to_thread(self.controller.entered.wait, 1)
            waiting = asyncio.create_task(self.server.call_tool("wait_task", {"task_id": task_id,
                                                                                 "timeout_seconds": 3}))
            await asyncio.sleep(0.01)
            await asyncio.wait_for(self.server.call_tool("cancel_task", {"task_id": task_id}), 1)
            ended = structured(await asyncio.wait_for(waiting, 1))
            self.assertEqual(ended["status"], "cancelled")
        self.assertEqual(captured.getvalue(), "")

    async def test_mcp_completed_result_points_to_persisted_answer(self):
        self.controller.release.set()
        result = structured(await self.server.call_tool("start_task", {"prompt": "answer", "cwd": str(self.cwd)}))
        task_id = result["task_id"]
        await self.server.call_tool("wait_task", {"task_id": task_id, "timeout_seconds": 2})
        response = structured(await self.server.call_tool("get_result", {"task_id": task_id}))
        self.assertEqual(Path(response["answer_path"]).read_text(), "answer")

    async def test_start_request_id_is_optional_and_retry_is_idempotent(self):
        tools = await self.server.list_tools()
        start = next(tool for tool in tools if tool.name == "start_task")
        self.assertIn("request_id", start.inputSchema["properties"])
        self.assertNotIn("request_id", start.inputSchema["required"])
        self.assertEqual(start.inputSchema["properties"]["model"]["default"], "deepseek-v4.1-flash")
        self.assertIn("context_tokens", start.inputSchema["properties"])
        self.assertNotIn("context_tokens", start.inputSchema["required"])
        arguments = {"prompt": "test", "cwd": str(self.cwd), "request_id": "mcp-request-001"}
        first = structured(await self.server.call_tool("start_task", arguments))
        await asyncio.to_thread(self.controller.entered.wait, 1)
        retry = structured(await self.server.call_tool("start_task", arguments))
        self.assertEqual(first["task_id"], retry["task_id"])
        self.assertEqual(len(self.controller.instances), 1)
        self.controller.release.set()
        await self.server.call_tool("wait_task", {"task_id": first["task_id"], "timeout_seconds": 2})

    async def test_read_only_initialization_health_works_while_task_directory_locked(self):
        # The explicit manager owns this actual flock, like the first Codex chat.
        with patch.dict("os.environ", {"WORKBUDDY_BRIDGE_STATE_DIR": str(self.manager.state_dir)}), \
                patch("workbuddy_bridge_intelmac.server.Manager", wraps=Manager) as constructor, \
                patch("workbuddy_bridge_intelmac.adapter.health", return_value={"available": True, "read_only": True}):
            second = create_server()
            async with second.settings.lifespan(second):
                tools = await second.list_tools()
                self.assertIn("health", {tool.name for tool in tools})
                self.assertEqual(structured(await second.call_tool("health", {})),
                                 {"available": True, "read_only": True})
                constructor.assert_not_called()
                with self.assertRaisesRegex(Exception, "只允许一个任务服务"):
                    await second.call_tool("start_task", {"prompt": "test", "cwd": str(self.cwd)})
                self.assertEqual(constructor.call_count, 1)
                self.assertEqual(self.manager._tasks, {})
                self.assertEqual(structured(await second.call_tool("health", {}))["available"], True)
                self.assertEqual(constructor.call_count, 1)
            # Closing an unused or failed second server must preserve the owner.
            self.assertIsNotNone(self.manager._lock_fd)

    async def test_concurrent_first_task_calls_create_only_one_manager(self):
        with patch("workbuddy_bridge_intelmac.server.Manager", return_value=self.manager) as constructor:
            lazy = create_server()
            await asyncio.gather(
                lazy.call_tool("get_task", {"task_id": "missing"}),
                lazy.call_tool("get_result", {"task_id": "missing"}),
                return_exceptions=True,
            )
            constructor.assert_called_once_with()

    async def test_lifespan_closes_only_initialized_manager(self):
        with patch("workbuddy_bridge_intelmac.server.Manager", return_value=self.manager) as constructor, \
                patch.object(self.manager, "close", wraps=self.manager.close) as close:
            unused = create_server()
            async with unused.settings.lifespan(unused):
                await unused.list_tools()
            constructor.assert_not_called()
            close.assert_not_called()
            used = create_server()
            async with used.settings.lifespan(used):
                with self.assertRaises(Exception):
                    await used.call_tool("get_task", {"task_id": "missing"})
            constructor.assert_called_once_with()
            close.assert_called_once_with()
