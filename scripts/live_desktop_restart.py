"""整应用正常退出/重启验收。默认只预览；--run 才执行真实任务和应用退出。

不调用 health，不强杀应用或 sidecar。脱敏报告写入忽略的 .local 目录。
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import plistlib
import re
import sys
import time
import uuid

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
LIVE = ROOT / '.local/live-desktop-restart'
APP = Path(os.environ.get('WORKBUDDY_APP', '/Applications/WorkBuddy.app'))
TERMINAL = {'succeeded', 'failed', 'cancelled', 'uncertain'}
PLAN = [
    '使用独立记录/工作目录，通过 MCP 提交默认 DeepSeek 的纯文本任务。',
    '确认 prompt_accepted 且尚未终止后，使用 AppleScript 正常退出 WorkBuddy。',
    '检查旧 GUI 进程退出，分别记录原 sidecar 是否保留，不强杀应用进程。',
    '记录原任务实际终态；任务可以继续完成，不推断退出必然导致失败。',
    '正常重新打开 WorkBuddy，通过只读 discover 确认认证，不调用 health。',
    '相同 request_id 重试原任务，确认返回原记录且没有重新提交。',
    '执行短文本恢复任务，并使用相同 request_id 重试验证幂等。',
    '保存脱敏结果；finally 尽力重新打开应用，只清理本脚本创建的 worker。',
]


def stamp():
    return datetime.now(timezone.utc).isoformat()


async def command(arguments, *, timeout=10, input_text=None):
    """只管理本函数直接创建的子进程；绝不向 GUI/sidecar 发送信号。"""
    child_env = dict(os.environ)
    child_env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + child_env.get('PYTHONPATH', '')
    process = await asyncio.create_subprocess_exec(*arguments, env=child_env,
        stdin=asyncio.subprocess.PIPE if input_text is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(
            input_text.encode() if input_text is not None else None), timeout)
        if process.returncode:
            raise RuntimeError('验收子步骤未正常结束')
        return output.decode('utf-8', errors='replace').strip()
    finally:
        if process.returncode is None:
            process.kill()  # Only this owned command/probe process.
            await process.wait()


async def call(session, name, arguments=None):
    result = await asyncio.wait_for(session.call_tool(name, arguments or {}), 65)
    if result.isError:
        raise RuntimeError('MCP 工具未正常返回：' + name)
    return result.structuredContent or json.loads(next(
        item.text for item in result.content if item.type == 'text'))


async def probe():
    # The child keeps credentials in memory. Its output excludes host/session IDs,
    # endpoints and captured buffers. PID is used internally, never in the report.
    code = ('import json; from workbuddy_bridge_intelmac.discovery import discover; '
            'd=discover(); print(json.dumps({"version":d.version,"protocol":d.protocol,'
            '"authenticated":True,"sidecar_pid":d.sidecar_pid}))')
    return json.loads(await command([sys.executable, '-c', code], timeout=25))


async def process_identity(pid):
    try:
        output = await command(['ps', '-p', str(pid), '-o', 'uid=,lstart=,comm='], timeout=5)
        return output or None
    except RuntimeError:
        return None


async def gui_identities(sidecar_pid):
    # Full arguments are inspected only in memory to distinguish the GUI from
    # Electron workers. They must never be logged or saved in the report.
    output = await command(['ps', '-axo', 'uid=,pid=,args='], timeout=5)
    prefix = str(APP / 'Contents/MacOS/Electron')
    found = {}
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) != 3 or parts[0] != str(os.getuid()):
            continue
        pid = int(parts[1])
        arguments = parts[2]
        if (pid == sidecar_pid or not (arguments == prefix or arguments.startswith(prefix + ' '))
                or '--serve' in arguments or 'sidecar' in arguments.lower()):
            continue
        identity = await process_identity(pid)
        if identity:
            found[pid] = identity
    return found


async def gui_running(bundle):
    output = await command(['osascript', '-e', f'application id "{bundle}" is running'], timeout=5)
    if output not in {'true', 'false'}:
        raise RuntimeError('无法确认应用运行状态')
    return output == 'true'


async def wait_accepted(session, task_id, timeout=220):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await call(session, 'get_task', {'task_id': task_id})
        if state['status'] in TERMINAL | {'permission_required'}:
            raise RuntimeError('原任务在应用退出前已经结束或申请权限，未完成在途验收')
        if state.get('prompt_accepted'):
            return state
        await asyncio.sleep(0.2)
    raise TimeoutError('未在期限内确认 prompt_accepted')


async def finish(session, task_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await call(session, 'wait_task', {'task_id': task_id, 'timeout_seconds': 5})
        if state['status'] in TERMINAL:
            return state, await call(session, 'get_result', {'task_id': task_id})
        if state['status'] == 'permission_required':
            await call(session, 'cancel_task', {'task_id': task_id})
            raise RuntimeError('纯文本验收出现权限请求；已取消，未自动授权')
    raise TimeoutError('任务终态未在期限内确认')


def same_task(before, after):
    return (before['task_id'] == after['task_id'] and before['created_at'] == after['created_at']
            and (before.get('runtime') or {}).get('worker_id') == (after.get('runtime') or {}).get('worker_id')
            and before.get('started_at') == after.get('started_at'))


def write_report(path, report):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


async def run(timeout_seconds=600):
    if sys.platform != 'darwin':
        raise RuntimeError('真实应用退出验收仅适用于 macOS')
    run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    LIVE.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(LIVE, 0o700)
    directory = LIVE / run_id
    directory.mkdir(parents=True, mode=0o700)
    cwd = directory / 'work'
    cwd.mkdir(mode=0o700)
    report_path = directory / 'report.json'
    report = {'started_at': stamp(), 'passed': False, 'stage': 'preflight',
              'calls_health': False, 'model': 'deepseek-v4.1-flash', 'cases': {}}
    owned = {}
    env = dict(os.environ)
    env['WORKBUDDY_BRIDGE_STATE_DIR'] = str(directory / 'tasks')
    env['PYTHONPATH'] = str(ROOT / 'src') + os.pathsep + env.get('PYTHONPATH', '')
    parameters = StdioServerParameters(command=sys.executable,
        args=['-m', 'workbuddy_bridge_intelmac.server'], env=env)
    bundle = plistlib.loads((APP / 'Contents/Info.plist').read_bytes())['CFBundleIdentifier']
    if not isinstance(bundle, str) or re.fullmatch(r'[A-Za-z0-9.-]+', bundle) is None:
        raise RuntimeError('应用标识格式无效')
    try:
        async with asyncio.timeout(timeout_seconds):
            initial = await probe()
            if not await gui_running(bundle):
                raise RuntimeError('验收前应用 GUI 未运行')
            old_gui = await gui_identities(initial['sidecar_pid'])
            if not old_gui:
                raise RuntimeError('无法识别原 GUI 主进程，暂不退出应用')
            old_sidecar = await process_identity(initial['sidecar_pid'])
            report['initial'] = {key: initial[key] for key in ('version', 'protocol', 'authenticated')}
            report['initial']['gui_process_count'] = len(old_gui)
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    try:
                        report['stage'] = 'start_interruption_task'
                        interrupted_args = {'cwd': str(cwd), 'request_id': 'desktop-interruption-' + run_id,
                            'prompt': '通信中断验收。不要使用工具、文件或网络。写一篇约1500字的中文研究方法教材，详细说明问题、资料、分析、写作和复核。'}
                        interrupted = await call(session, 'start_task', interrupted_args)
                        owned[interrupted['task_id']] = None
                        accepted = await wait_accepted(session, interrupted['task_id'])
                        owned[interrupted['task_id']] = accepted.get('runtime')
                        report['stage'] = 'normal_quit'
                        await command(['osascript', '-e', f'tell application id "{bundle}" to quit'], timeout=15)
                        deadline = time.monotonic() + 40
                        exited = False
                        while time.monotonic() < deadline:
                            still_old = [pid for pid, identity in old_gui.items()
                                         if await process_identity(pid) == identity]
                            if not still_old and not await gui_running(bundle):
                                exited = True
                                break
                            await asyncio.sleep(1)
                        sidecar_exited = await process_identity(initial['sidecar_pid']) != old_sidecar
                        report['cases']['normal_quit'] = {'gui_exited': exited,
                            'old_sidecar_exited': sidecar_exited, 'passed': exited,
                            'non_owned_processes_signalled': False}
                        write_report(report_path, report)
                        if not exited:
                            raise RuntimeError('应用未正常退出；未强制结束，实际退出验收未完成')
                        report['stage'] = 'reopen_and_authenticate'
                        await command(['open', '-a', str(APP)], timeout=10)
                        deadline = time.monotonic() + 100
                        restored = None
                        while time.monotonic() < deadline:
                            try:
                                restored = await probe()
                                break
                            except (RuntimeError, TimeoutError, json.JSONDecodeError):
                                await asyncio.sleep(1)
                        if restored is None:
                            raise TimeoutError('应用重开后认证通道未在期限内恢复')
                        report['cases']['reopen'] = {'authenticated': True,
                            'new_sidecar': restored['sidecar_pid'] != initial['sidecar_pid'],
                            'version': restored['version'], 'protocol': restored['protocol'], 'passed': True}
                        report['stage'] = 'interrupted_task_outcome'
                        state, result = await finish(session, interrupted['task_id'], timeout=60)
                        owned[interrupted['task_id']] = state.get('runtime')
                        retried = await call(session, 'start_task', interrupted_args)
                        idempotent = same_task(state, retried)
                        report['cases']['interrupted_task'] = {'status': state['status'],
                            'partial': bool(result.get('partial')), 'same_record_on_retry': idempotent,
                            'passed': state['status'] in {'uncertain', 'failed', 'succeeded'} and idempotent}
                        report['stage'] = 'short_task_after_restart'
                        short_args = {'cwd': str(cwd), 'request_id': 'desktop-restored-' + run_id,
                            'prompt': '桥接恢复验收。不要使用工具、文件或网络。只回复：INTELMAC_DESKTOP_RESTART_OK'}
                        short = await call(session, 'start_task', short_args)
                        owned[short['task_id']] = None
                        state, result = await finish(session, short['task_id'], timeout=220)
                        owned[short['task_id']] = state.get('runtime')
                        retried = await call(session, 'start_task', short_args)
                        idempotent = same_task(state, retried)
                        report['cases']['short_task'] = {'status': state['status'],
                            'expected_response': result.get('summary', '').strip() == 'INTELMAC_DESKTOP_RESTART_OK',
                            'same_record_on_retry': idempotent,
                            'passed': state['status'] == 'succeeded' and idempotent
                            and result.get('summary', '').strip() == 'INTELMAC_DESKTOP_RESTART_OK'}
                        report['passed'] = all(case['passed'] for case in report['cases'].values())
                    finally:
                        try:
                            async with asyncio.timeout(12):
                                for task_id in owned:
                                    try:
                                        state = await call(session, 'get_task', {'task_id': task_id})
                                        owned[task_id] = state.get('runtime') or owned[task_id]
                                        if state['status'] not in TERMINAL:
                                            await call(session, 'cancel_task', {'task_id': task_id})
                                    except Exception:
                                        pass
                        except TimeoutError:
                            report['mcp_cleanup_timed_out'] = True
    except Exception as error:
        report['error_type'] = type(error).__name__
        # Do not emit exception strings: transport errors can contain private data.
    finally:
        # Cleanup has its own bounded allowance, even if the main deadline expired.
        report['cleanup'] = {'reopen_attempted': True, 'owned_workers': len(owned)}
        try:
            await command(['open', '-a', str(APP)], timeout=8)
            report['cleanup']['reopen_command_succeeded'] = True
        except Exception as error:
            report['cleanup']['reopen_error_type'] = type(error).__name__
        code = ('import sys,json; from workbuddy_bridge_intelmac.adapter import recover_runtime; '
                'r=recover_runtime(json.load(sys.stdin)); print(json.dumps({"cleaned":bool(r.get("cleaned"))}))')
        report['cleanup']['workers'] = []
        for runtime in owned.values():
            if not runtime:
                report['cleanup']['workers'].append({'identity_available': False})
                continue
            try:
                result = json.loads(await command([sys.executable, '-c', code], timeout=15,
                                                  input_text=json.dumps(runtime)))
                report['cleanup']['workers'].append(result)
            except Exception as error:
                report['cleanup']['workers'].append({'error_type': type(error).__name__})
        report['finished_at'] = stamp()
        write_report(report_path, report)
        write_report(LIVE / 'report.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--run', action='store_true', help='执行真实模型任务并正常退出/重开 WorkBuddy')
    group.add_argument('--dry-run', action='store_true', help='仅预览，不访问应用或发起任务（默认）')
    parser.add_argument('--timeout-seconds', type=int, default=600,
                        help='主流程总时限，120–900秒；MCP清理另限12秒，应用恢复/自有worker清理另限38秒')
    args = parser.parse_args()
    if not 120 <= args.timeout_seconds <= 900:
        parser.error('总时限必须在120到900秒之间')
    if not args.run:
        print(json.dumps({'dry_run': True, 'calls_health': False, 'steps': PLAN}, ensure_ascii=False, indent=2))
        return 0
    return 0 if asyncio.run(run(args.timeout_seconds)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
