"""Explicit Intel Mac acceptance of worker loss, invalid auth, and bridge restart.

Uses short, interrupted model tasks in .local/live-faults. Never exits WorkBuddy
or signals an existing desktop session. Only our spawned MCP process is killed.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from workbuddy_bridge_intelmac.adapter import recover_runtime
from workbuddy_bridge_intelmac.discovery import WorkBuddyError, auth_status

ROOT=Path(__file__).resolve().parents[1]
LIVE=ROOT/'.local/live-faults'
TERMINAL={'succeeded','failed','cancelled','uncertain'}

async def call(session,name,arguments=None):
    result=await session.call_tool(name,arguments or {})
    if result.isError:raise RuntimeError('MCP tool failed: '+name)
    return result.structuredContent or json.loads(next(x.text for x in result.content if x.type=='text'))

async def flowing(session,task):
    deadline=time.monotonic()+120
    while time.monotonic()<deadline:
        state=await call(session,'get_task',{'task_id':task['task_id']})
        if state.get('prompt_accepted'):return state
        if state['status'] in TERMINAL|{'permission_required'}:raise RuntimeError('Fault test ended before streaming')
        await asyncio.sleep(0.2)
    await call(session,'cancel_task',{'task_id':task['task_id']})
    raise TimeoutError('No model output within test deadline')

async def run():
    LIVE.mkdir(parents=True,exist_ok=True,mode=0o700)
    env=dict(os.environ)
    env['WORKBUDDY_BRIDGE_STATE_DIR']=str(LIVE/'tasks')
    parameters=StdioServerParameters(command=sys.executable,args=['-m','workbuddy_bridge_intelmac.server'],env=env)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'cases':{}}
    prompt='不要调用工具或网络。请写一篇1500字的中文研究方法教材，详述研究问题、假设、资料收集、分析、写作与复核。'
    directory=LIVE/'work';directory.mkdir(exist_ok=True)
    print('Starting worker loss and invalid auth',flush=True)
    async with stdio_client(parameters) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            task=await call(session,'start_task',{'prompt':prompt,'cwd':str(directory)})
            state=await flowing(session,task)
            try:auth_status(state['runtime']['endpoint'],'intentionally-invalid-test-credential')
            except WorkBuddyError:report['cases']['invalid_auth']={'passed':True}
            else:report['cases']['invalid_auth']={'passed':False}
            cleanup=recover_runtime(state['runtime'])
            deadline=time.monotonic()+30
            while time.monotonic()<deadline:
                state=await call(session,'wait_task',{'task_id':task['task_id'],'timeout_seconds':5})
                if state['status'] in TERMINAL:break
            result=await call(session,'get_result',{'task_id':task['task_id']})
            health=await call(session,'health')
            report['cases']['worker_loss']={'task_id':task['task_id'],'status':state['status'],
                'partial':result['partial'],'answer_path':result['answer_path'],'desktop_still_available':health['available'],
                'cleanup_confirmed':cleanup.get('cleaned'),
                'passed':state['status']=='uncertain' and health['available'] and cleanup.get('cleaned')}
    print(json.dumps(report['cases']),flush=True)
    (LIVE/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print('Starting forced bridge restart',flush=True)
    pidfile=LIVE/'owned-mcp.pid'
    if pidfile.exists():pidfile.unlink()
    wrapper="from pathlib import Path;import os;from workbuddy_bridge_intelmac.server import main;Path("+repr(str(pidfile))+").write_text(str(os.getpid()));main()"
    owned=StdioServerParameters(command=sys.executable,args=['-c',wrapper],env=env)
    task=None
    try:
        async with stdio_client(owned) as (reader,writer):
            async with ClientSession(reader,writer) as session:
                await session.initialize()
                task=await call(session,'start_task',{'prompt':prompt,'cwd':str(directory)})
                state=await flowing(session,task)
                pid=int(pidfile.read_text())
                command=subprocess.run(['ps','-p',str(pid),'-o','command='],capture_output=True,text=True,check=True).stdout
                if pid<=1 or str(pidfile) not in command or 'workbuddy_bridge_intelmac.server' not in command:
                    raise RuntimeError('MCP process identity does not match the test')
                os.kill(pid,signal.SIGKILL)
                await asyncio.sleep(1.5)
    except Exception as error:
        # Process death can make the stdio transport report an expected EOF.
        report['transport_exit_type']=type(error).__name__
    if not task:raise RuntimeError('Restart test did not start a task')
    async with stdio_client(parameters) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            state=await call(session,'get_task',{'task_id':task['task_id']})
            health=await call(session,'health')
            report['cases']['bridge_restart']={'task_id':task['task_id'],'status':state['status'],
                'recovery':state.get('recovery'),'desktop_still_available':health['available'],
                'passed':state['status']=='uncertain' and state.get('recovery',{}).get('cleaned') is True and health['available']}
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    (LIVE/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    print(json.dumps(report,ensure_ascii=False,indent=2),flush=True)
    return all(case['passed'] for case in report['cases'].values())

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run',action='store_true')
    p.add_argument('--output',type=Path,default=LIVE,help='独立验收记录目录')
    args=p.parse_args()
    LIVE=args.output.resolve()
    if not args.run:p.error('Use --run to execute real model tasks')
    raise SystemExit(0 if asyncio.run(run()) else 1)
