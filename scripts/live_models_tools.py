"""Explicit subscription-backed model/context and read-only tool inheritance check."""
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

ROOT=Path(__file__).resolve().parents[1]
LIVE=ROOT/'.local/model-tools'
TERMINAL={'succeeded','failed','cancelled','uncertain'}
MODELS=('deepseek-v4.1-flash','glm-5.3-flash')

async def call(session,name,args=None):
    result=await session.call_tool(name,args or {})
    if result.isError:raise RuntimeError('MCP call failed: '+name)
    return result.structuredContent or json.loads(next(x.text for x in result.content if x.type=='text'))

async def run():
    LIVE.mkdir(parents=True,exist_ok=True,mode=0o700)
    env=dict(os.environ,WORKBUDDY_BRIDGE_STATE_DIR=str(LIVE/'tasks'))
    parameters=StdioServerParameters(command=sys.executable,args=['-m','workbuddy_bridge_intelmac.server'],env=env)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'cases':{}}
    prompt='''这是只读桥接能力验收，请严格限制动作：
1. 若存在 ToolSearch 工具，实际调用它查找通达信 tdx_lookup_stock、同花顺 westock、法律 fazhi-law/pkulaw 相关工具。只做工具发现，不调用这些第三方服务，不交易、不修改任何内容。若没有该工具，明确报告不存在，禁止改用 Bash、网页或读取配置来搜索。
2. 若存在 Skill 工具，实际调用它仅加载用户现有 find-skills 技能，然后从加载内容提取 version 和 slug。不要执行技能建议的搜索、安装或其他动作。若不存在该技能，如实报告，不另行创建。
3. 返回简短 JSON，含 discovered_tools、skill_loaded、skill_version、skill_slug、limitations。禁止读取凭证，禁止外发消息，禁止修改配置或文件。不要根据提示猜测工具或技能已经加载。'''
    async with stdio_client(parameters) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            report['health']=await call(session,'health')
            for model in MODELS:
                work=LIVE/model;work.mkdir(exist_ok=True)
                print('Testing '+model,flush=True)
                task=await call(session,'start_task',{'prompt':prompt,'cwd':str(work),
                    'model':model,'context_tokens':1000000})
                deadline=time.monotonic()+240
                permissions=[]
                while time.monotonic()<deadline:
                    state=await call(session,'wait_task',{'task_id':task['task_id'],'timeout_seconds':15})
                    if state['status']=='permission_required':
                        request=state['permission_request']
                        # No permission approvals: this pass only discovers tool
                        # metadata and loads Skill instructions. Anything else is
                        # rejected and recorded for human review.
                        options=request.get('options',[])
                        deny=next((x for x in options if x.get('kind')=='reject_once'),None)
                        permissions.append({'tool_name':request.get('tool_name'),'decision':'reject_once'})
                        if deny:
                            await call(session,'respond_permission',{'task_id':task['task_id'],
                               'request_id':request['request_id'],'option_id':deny['optionId']})
                        else:await call(session,'cancel_task',{'task_id':task['task_id']})
                    if state['status'] in TERMINAL:break
                else:
                    await call(session,'cancel_task',{'task_id':task['task_id']})
                    state=await call(session,'wait_task',{'task_id':task['task_id'],'timeout_seconds':30})
                result=await call(session,'get_result',{'task_id':task['task_id']})
                report['cases'][model]={'task_id':task['task_id'],'status':result['status'],
                    'model':result['model'],'usage':result.get('usage'),'activities':result.get('activities',[]),
                    'available_skills':result.get('available_skills',[]),'summary':result['summary'],
                    'answer_path':result['answer_path'],'permissions':permissions,
                    'model_context_passed':result['status']=='succeeded' and result['model']['id']==model
                      and result['model']['context_tokens']==1000000
                      and (result.get('usage') or {}).get('context_size')==1000000}
                (LIVE/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
                print(json.dumps({'model':model,'status':result['status'],'usage':result.get('usage'),'tools':sorted({x.get('tool_name','') for x in result.get('activities',[])}),'answer_path':result['answer_path']},ensure_ascii=False),flush=True)
    report['finished_at']=datetime.now(timezone.utc).isoformat()
    (LIVE/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if not all(x['model_context_passed'] for x in report['cases'].values()):
        raise RuntimeError('Some model/context checks did not pass; inspect report')

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',action='store_true',help='Submit two short real tasks using current WorkBuddy credits')
    args=parser.parse_args()
    if not args.run:parser.error('Use --run to authorize real model requests')
    asyncio.run(run())
