"""整应用正常退出/重启验收。默认只预览；--run 才执行真实任务和应用退出。

不调用 MCP health；仅执行只读健康探测，不强杀应用或 sidecar。脱敏报告写入忽略的 .local 目录。
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
    '等待原任务 uncertain 与自有资源清理确认；意外成功不算中断验收通过。',
    '重新打开 WorkBuddy，只读确认唯一 GUI 与 ready_for_task；此时不宣称认证通过。',
    '相同 request_id 重试原任务，确认返回原记录且没有重新提交。',
    '新自有 worker 短任务成功并清理确认后，再确认认证恢复；重复请求验证幂等。',
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
    # Only public readiness and an internal GUI identity are returned. A cold
    # desktop has no authenticated runtime yet; this probe never creates one.
    code = (
        'import json; from workbuddy_bridge_intelmac.adapter import health; '
        'from workbuddy_bridge_intelmac.owned_runtime import gui_identity; '
        'from workbuddy_bridge_intelmac.discovery import find_sockets; '
        'h=health(); g=gui_identity(); '
        'r={k:h.get(k) for k in ("available","version","protocol","authenticated","supported","execution_mode")}; '
        'r["ready_for_task"]=h.get("ready_for_task",h.get("available",False)); '
        'r["runtime_ready"]=h.get("runtime_ready",h.get("authenticated") is True); '
        'r["gui"]=g; r["sidecar_pids"]=sorted({pid for _,pid in find_sockets()}); '
        'print(json.dumps(r))')
    return json.loads(await command([sys.executable, '-c', code], timeout=25))


async def process_identity(pid):
    try:
        output = await command(['ps', '-p', str(pid), '-o', 'uid=,lstart=,comm='], timeout=5)
        return output or None
    except RuntimeError:
        return None


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
            if state.get('cleanup_confirmed') is True and state.get('cleanup_pending') is not True:
                return state, await call(session, 'get_result', {'task_id': task_id})
            # wait_task returns immediately for terminal tasks; poll without spinning.
            await asyncio.sleep(0.2)
            continue
        if state['status'] == 'permission_required':
            await call(session, 'cancel_task', {'task_id': task_id})
            raise RuntimeError('纯文本验收出现权限请求；已取消，未自动授权')
    raise TimeoutError('任务终态与清理未在期限内确认')


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


async def wait_permission(session, task_id, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = await call(session, 'get_task', {'task_id': task_id})
        if state['status'] == 'permission_required' and state.get('permission_request', {}).get('kind') == 'execute':
            return state
        if state['status'] in TERMINAL:
            raise RuntimeError('未进入工具权限等待，不能执行本项退出验收')
        await asyncio.sleep(0.2)
    raise TimeoutError('权限等待验收超时')


async def run(timeout_seconds=600, interrupt_mode='stream'):
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
              'calls_mcp_health': False, 'uses_read_only_health_probe': True, 'interrupt_mode': interrupt_mode, 'model': 'deepseek-v4.1-flash', 'cases': {}}
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
            if initial.get('ready_for_task') is not True:
                raise RuntimeError('桌面尚未准备好接受任务')
            gui = initial['gui']
            if type(gui.get('pid')) is not int or gui['pid'] <= 1 or not isinstance(gui.get('start'), str):
                raise RuntimeError('无法识别唯一原 GUI 主进程，暂不退出应用')
            gui_process = await process_identity(gui['pid'])
            if not gui_process:
                raise RuntimeError('原 GUI 进程身份已失效，暂不退出应用')
            old_gui = {gui['pid']: gui_process}
            old_sidecars = {pid: await process_identity(pid) for pid in initial.get('sidecar_pids', [])}
            report['initial'] = {key: initial.get(key) for key in
                                ('version', 'protocol', 'authenticated', 'ready_for_task', 'runtime_ready')}
            report['initial']['gui_process_count'] = 1
            report['initial']['old_sidecar_present'] = bool(old_sidecars)
            async with stdio_client(parameters) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    try:
                        report['stage'] = 'start_interruption_task'
                        interrupted_args = {'cwd': str(cwd), 'request_id': 'desktop-interruption-' + run_id,
                            'prompt': '通信中断验收。不要使用工具、文件或网络。写一篇约1500字的中文研究方法教材，详细说明问题、资料、分析、写作和复核。'}
                        if interrupt_mode == 'permission':
                            target = cwd / 'permission-must-not-exist.txt'
                            interrupted_args['prompt'] = f'权限等待中断测试。只用 Bash 尝试执行 printf PERMISSION_TEST > "{target}"，保留权限请求，不使用其他工具或访问网络。'
                        interrupted = await call(session, 'start_task', interrupted_args)
                        owned[interrupted['task_id']] = None
                        accepted = await (wait_permission(session, interrupted['task_id']) if interrupt_mode == 'permission'
                                          else wait_accepted(session, interrupted['task_id']))
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
                        sidecar_exited = (all([await process_identity(pid) != identity
                                              for pid, identity in old_sidecars.items()]) if old_sidecars else None)
                        report['cases']['normal_quit'] = {'gui_exited': exited,
                            'old_sidecar_exited': sidecar_exited, 'passed': exited,
                            'non_owned_processes_signalled': False}
                        write_report(report_path, report)
                        if not exited:
                            raise RuntimeError('应用未正常退出；未强制结束，实际退出验收未完成')
                        report['stage'] = 'interrupted_task_outcome'
                        state, result = await finish(session, interrupted['task_id'], timeout=60)
                        owned[interrupted['task_id']] = state.get('runtime')
                        retried = await call(session, 'start_task', interrupted_args)
                        idempotent = same_task(state, retried)
                        report['cases']['interrupted_task'] = {'status': state['status'],
                            'partial': bool(result.get('partial')), 'same_record_on_retry': idempotent,
                            'permission_cleared': state.get('permission_request') is None,
                            'permission_test_file_absent': interrupt_mode != 'permission' or not target.exists(),
                            'cleanup_confirmed': state.get('cleanup_confirmed') is True,
                            'passed': state['status'] == 'uncertain' and idempotent
                            and state.get('permission_request') is None
                            and (interrupt_mode != 'permission' or not target.exists())
                            and state.get('cleanup_confirmed') is True and not state.get('cleanup_pending')}
                        write_report(report_path, report)
                        report['stage'] = 'reopen_and_ready'
                        await command(['open', '-a', str(APP)], timeout=10)
                        deadline = time.monotonic() + 100
                        restored = None
                        while time.monotonic() < deadline:
                            try:
                                candidate = await probe()
                                if candidate.get('ready_for_task') is True and await gui_running(bundle):
                                    restored = candidate
                                    break
                            except (RuntimeError, TimeoutError, json.JSONDecodeError):
                                pass
                            await asyncio.sleep(1)
                        if restored is None:
                            raise TimeoutError('应用重开后未在期限内确认唯一 GUI 和任务就绪')
                        report['cases']['reopen'] = {'ready_for_task': True,
                            'authenticated': None, 'authentication_source': 'not_verified_yet',
                            'new_gui': restored['gui'] != initial['gui'],
                            'version': restored.get('version'), 'protocol': restored.get('protocol'), 'passed': False}
                        report['stage'] = 'short_task_after_restart'
                        short_args = {'cwd': str(cwd), 'request_id': 'desktop-restored-' + run_id,
                            'prompt': '桥接恢复验收。不要使用工具、文件或网络。只回复：INTELMAC_DESKTOP_RESTART_OK'}
                        short = await call(session, 'start_task', short_args)
                        owned[short['task_id']] = None
                        state, result = await finish(session, short['task_id'], timeout=220)
                        owned[short['task_id']] = state.get('runtime')
                        retried = await call(session, 'start_task', short_args)
                        idempotent = same_task(state, retried)
                        owned_launcher = (state.get('runtime') or {}).get('launcher') == 'owned'
                        short_passed = (state['status'] == 'succeeded' and idempotent and owned_launcher
                                        and state.get('cleanup_confirmed') is True and not state.get('cleanup_pending')
                                        and result.get('summary', '').strip() == 'INTELMAC_DESKTOP_RESTART_OK')
                        report['cases']['short_task'] = {'status': state['status'],
                            'expected_response': result.get('summary', '').strip() == 'INTELMAC_DESKTOP_RESTART_OK',
                            'same_record_on_retry': idempotent, 'owned_launcher': owned_launcher,
                            'cleanup_confirmed': state.get('cleanup_confirmed') is True, 'passed': short_passed}
                        if short_passed:
                            report['cases']['reopen'].update(authenticated=True,
                                authentication_source='new_owned_worker_task',
                                passed=report['cases']['reopen']['new_gui'])
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
                'r=recover_runtime(json.load(sys.stdin)); print(json.dumps({"cleaned":r.get("cleaned") is True}))')
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
        report['cleanup']['passed'] = cleanup_passed(report)
        report['passed'] = report['passed'] and report['cleanup']['passed'] and 'error_type' not in report
        report['finished_at'] = stamp()
        write_report(report_path, report)
        write_report(LIVE / 'report.json', report)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return report['passed']


def cleanup_passed(report):
    cleanup = report.get('cleanup', {})
    workers = cleanup.get('workers', [])
    return (cleanup.get('reopen_command_succeeded') is True
            and not report.get('mcp_cleanup_timed_out')
            and len(workers) == cleanup.get('owned_workers', -1)
            and all(worker.get('cleaned') is True for worker in workers))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--run', action='store_true', help='执行真实模型任务并正常退出/重开 WorkBuddy')
    group.add_argument('--dry-run', action='store_true', help='仅预览，不访问应用或发起任务（默认）')
    parser.add_argument('--timeout-seconds', type=int, default=600,
                        help='主流程总时限，120–900秒；MCP清理另限12秒，应用恢复/自有worker清理另限38秒')
    parser.add_argument('--interrupt-mode', choices=['stream','permission'], default='stream')
    args = parser.parse_args()
    if not 120 <= args.timeout_seconds <= 900:
        parser.error('总时限必须在120到900秒之间')
    if not args.run:
        print(json.dumps({'dry_run': True, 'calls_health': False, 'steps': PLAN}, ensure_ascii=False, indent=2))
        return 0
    return 0 if asyncio.run(run(args.timeout_seconds,args.interrupt_mode)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
