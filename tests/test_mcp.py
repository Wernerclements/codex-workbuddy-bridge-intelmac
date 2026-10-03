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
        arguments = {"prompt": "test", "cwd": str(self.cwd), "request_id": "mcp-request-001"}
        first = structured(await self.server.call_tool("start_task", arguments))
        await asyncio.to_thread(self.controller.entered.wait, 1)
        retry = structured(await self.server.call_tool("start_task", arguments))
        self.assertEqual(first["task_id"], retry["task_id"])
        self.assertEqual(len(self.controller.instances), 1)
        self.controller.release.set()
        await self.server.call_tool("wait_task", {"task_id": first["task_id"], "timeout_seconds": 2})
