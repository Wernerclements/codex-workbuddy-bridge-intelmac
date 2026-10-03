"""Explicit acceptance over real MCP stdio. Uses existing WorkBuddy model usage.

Records and artifacts stay in ignored .local/live. Does not configure an API.
"""
from __future__ import annotations
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / '.local/live'
TERMINAL = {'succeeded', 'failed', 'cancelled', 'uncertain'}

async def call(session, name, arguments=None):
    result = await session.call_tool(name, arguments or {})
    if result.isError:
        raise RuntimeError(f'MCP tool failed: {name}')
    if result.structuredContent:
        return result.structuredContent
    return json.loads(next(item.text for item in result.content if item.type == 'text'))

def timestamp():
    return datetime.now(timezone.utc).isoformat()

async def finish(session, task, *, policy='reject', cwd=None, timeout=180):
    deadline = time.monotonic() + timeout
    requests = []
    while time.monotonic() < deadline:
        state = await call(session, 'wait_task', {'task_id': task['task_id'], 'timeout_seconds': 10})
        if state['status'] in TERMINAL:
            return state, await call(session, 'get_result', {'task_id': task['task_id']}), requests
        if state['status'] == 'permission_required':
            request = state['permission_request']
            locations = request.get('locations') or []
            in_scope = cwd is not None and bool(locations) and all(isinstance(item.get('path'), str)
                and Path(item['path']).resolve().is_relative_to(cwd.resolve()) for item in locations)
            permit = policy == 'test_file' and in_scope and request.get('kind') in {'edit', 'write'}
            kind = 'allow_once' if permit else 'reject_once'
            choice = next((option['optionId'] for option in request['options'] if option['kind'] == kind), None)
            if not choice:
                await call(session, 'cancel_task', {'task_id': task['task_id']})
                raise RuntimeError('Permission options do not include the test policy choice')
            requests.append({'kind': request.get('kind'), 'choice': kind})
            await call(session, 'respond_permission', {'task_id': task['task_id'],
                'request_id': request['request_id'], 'option_id': choice})
    await call(session, 'cancel_task', {'task_id': task['task_id']})
    raise TimeoutError('Acceptance task exceeded deadline')

async def run(cases):
    LIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    env['WORKBUDDY_BRIDGE_STATE_DIR'] = str(LIVE / 'tasks')
    report = {'started_at': timestamp(), 'cases': {}}
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'workbuddy_bridge_intelmac.server'], env=env)
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            await session.initialize()
            report['tool_count'] = len((await session.list_tools()).tools)
            report['health'] = await call(session, 'health')
            if not report['health']['available']:
                raise RuntimeError('WorkBuddy is unavailable')
            for case in cases:
                directory = LIVE / case
                directory.mkdir(exist_ok=True)
                entry = {'started_at': timestamp()}
                print(f'Starting {case}', flush=True)
                if case == 'text':
                    task = await call(session, 'start_task', {'cwd': str(directory),
                        'prompt': '桥接通信验收。不要调用工具、访问文件或网络。请仅回复：INTELMAC_MCP_OK'})
                    early = await call(session, 'wait_task', {'task_id': task['task_id'], 'timeout_seconds': 0.01})
                    entry['wait_timeout_kept_task'] = early['wait_timed_out'] and not early['cancel_requested']
                    state, result, permissions = await finish(session, task)
                    entry.update(task_id=task['task_id'], status=state['status'], permissions=permissions,
                        result=result, passed=state['status']=='succeeded' and result['summary'].strip()=='INTELMAC_MCP_OK')
                elif case == 'file':
                    target = directory / 'bridge-result.txt'
                    if target.exists(): target.unlink()
                    task = await call(session, 'start_task', {'cwd': str(directory), 'prompt':
                        f'文件回收验收。使用写入文件工具在 {target} 新建一个纯文本文件，完整内容为 INTELMAC_FILE_OK。'
                        '只创建这个文件，不调用 shell 或网络、不访问其他文件。完成后简短说明路径。'})
                    state, result, permissions = await finish(session, task, policy='test_file', cwd=directory)
                    entry.update(task_id=task['task_id'], status=state['status'], permissions=permissions, result=result,
                        passed=state['status']=='succeeded' and target.is_file() and target.read_text().strip()=='INTELMAC_FILE_OK'
                        and any(item['path']==str(target) for item in result['artifacts']))
                elif case == 'cancel':
                    task = await call(session, 'start_task', {'cwd': str(directory), 'prompt':
                        '取消测试。不要调用工具、访问文件或网络。连续输出2000条不同的中文研究问题，每条详细解释，尽量完整。'})
                    deadline = time.monotonic() + 120
                    while time.monotonic() < deadline:
                        state = await call(session, 'get_task', {'task_id': task['task_id']})
                        if state.get('answer_chars',0)>0 or state['status'] in TERMINAL|{'permission_required'}: break
                        await asyncio.sleep(0.5)
                    entry['model_output_started'] = state.get('answer_chars',0)>0
                    await call(session, 'cancel_task', {'task_id': task['task_id']})
                    state, result, permissions = await finish(session, task, timeout=45)
                    entry.update(task_id=task['task_id'], status=state['status'], result=result,
                        passed=state['status']=='cancelled' and entry['model_output_started'])
                elif case == 'permission':
                    outside = LIVE / 'permission-outside.txt'
                    if outside.exists(): outside.unlink()
                    task = await call(session, 'start_task', {'cwd': str(directory), 'prompt':
                        f'权限拒绝验收。尝试使用 Bash 工具执行命令 printf INTELMAC_PERMISSION_TEST > "{outside}"。'
                        '该文件位于工作目录之外。只尝试一次，如果权限被拒绝就停止并说明，不换工具或再次申请权限。'})
                    state, result, permissions = await finish(session, task)
                    entry.update(task_id=task['task_id'], status=state['status'], permissions=permissions, result=result,
                        outside_file_created=outside.exists(), passed=bool(permissions)
                        and all(item['choice']=='reject_once' for item in permissions) and not outside.exists())
                else: raise ValueError('Unknown case')
                entry['finished_at'] = timestamp()
                report['cases'][case] = entry
                report['updated_at'] = timestamp()
                (LIVE / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
                print(json.dumps({'case':case,'passed':entry['passed'],'status':entry['status']}),flush=True)
    report['finished_at'] = timestamp()
    (LIVE / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return all(item['passed'] for item in report['cases'].values())

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Run real model tasks using the WorkBuddy account')
    parser.add_argument('--cases',nargs='+',choices=['text','file','cancel','permission'],default=['text','file','cancel','permission'])
    args=parser.parse_args()
    if not args.run: parser.error('Use --run to execute real model tasks')
    raise SystemExit(0 if asyncio.run(run(args.cases)) else 1)
