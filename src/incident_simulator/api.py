"""Authenticated task service, same-origin frontend and replayable SSE."""
import asyncio
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal
from uuid import uuid4
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from . import database as d, auth

SCENARIOS = {'db_pool_exhausted':'数据库连接池耗尽', 'disk_full':'日志磁盘占满', 'data_corruption':'订单数据损坏'}
TERMINAL = {'succeeded','failed','cancelled','timed_out','incomplete','needs_review'}
app = FastAPI(title='IncidentSimulator', version='0.3.0')
User = Depends(auth.current_user)
login_attempts = defaultdict(deque)

class Input(BaseModel):
    model_config = ConfigDict(extra='forbid')
class Login(Input):
    username: str = Field(min_length=1,max_length=80)
    password: str = Field(min_length=1,max_length=200)
class NewTask(Input):
    scenario: str
    mode: Literal['fixture','cloud'] = 'fixture'
class Decision(Input):
    operation_id: str
    digest: str
    approve: bool
    comment: str = Field(default='',max_length=2000)
class NewUser(Input):
    username: str = Field(min_length=3,max_length=80)
    password: str = Field(min_length=12,max_length=200)
    role: Literal['admin','operator','viewer'] = 'operator'

def public(job):
    return {k:getattr(job,k) for k in ['id','scenario','mode','status','created','updated','trace_id','error','revision']} | {
        'approval':json.loads(job.approval) if job.approval else None,
        'report':json.loads(job.report) if job.report else None}

@app.get('/api/health')
def health():
    with d.Session() as db:
        seen = db.scalar(select(d.WorkerHeartbeat.seen).order_by(d.WorkerHeartbeat.seen.desc()).limit(1))
    return {'status':'ok','database':d.engine.dialect.name,'worker_online':bool(seen and time.time()-seen<30),
            'cloud_enabled':os.getenv('ENABLE_CLOUD_MODEL') == 'true'}

@app.post('/api/auth/login')
def login(data: Login, request: Request, response: Response):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403,'Cross-origin login denied')
    key = request.client.host if request.client else 'local'
    attempts = login_attempts[key]
    while attempts and attempts[0] < time.time()-60: attempts.popleft()
    if len(attempts)>=10: raise HTTPException(429,'登录尝试过多，请稍后重试')
    attempts.append(time.time())
    user, token = auth.login(data.username,data.password)
    response.set_cookie(auth.COOKIE,token,httponly=True,samesite='strict',secure=os.getenv('COOKIE_SECURE')=='true',max_age=28800)
    return {'username':user.username,'role':user.role,'csrf':auth.csrf(token)}

@app.get('/api/auth/me')
def me(request: Request,user=User):
    return {'username':user.username,'role':user.role,'csrf':auth.csrf(request.cookies[auth.COOKIE])}

@app.post('/api/auth/logout')
def logout(request: Request,response: Response,user=User):
    with d.transaction() as db:
        session=db.get(d.LoginSession,auth.digest(request.cookies[auth.COOKIE]))
        if session: db.delete(session)
    response.delete_cookie(auth.COOKIE)
    return {'ok':True}

@app.get('/api/users')
def users(user=User):
    if user.role!='admin': raise HTTPException(403,'Admin required')
    with d.Session() as db:
        return [{'id':u.id,'username':u.username,'role':u.role,'enabled':u.enabled} for u in db.scalars(select(d.User))]

@app.post('/api/users',status_code=201)
def new_user(data:NewUser,user=User):
    if user.role!='admin': raise HTTPException(403,'Admin required')
    try: created=auth.create_user(data.username,data.password,data.role)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    return {'id':created.id,'username':created.username,'role':created.role}

@app.get('/api/scenarios')
def scenarios(user=User): return [{'id':k,'name':v} for k,v in SCENARIOS.items()]

@app.post('/api/tasks',status_code=201)
def create(data:NewTask,user=User):
    if user.role not in {'admin','operator'}: raise HTTPException(403,'需要操作员权限')
    if data.scenario not in SCENARIOS: raise HTTPException(422,'Unknown scenario')
    if data.mode=='cloud' and os.getenv('ENABLE_CLOUD_MODEL')!='true': raise HTTPException(409,'付费模型调用已关闭')
    with d.transaction() as db:
        job=d.Job(id=str(uuid4()),owner_id=user.id,scenario=data.scenario,mode=data.mode,status='queued',
            created=time.time(),updated=time.time(),trace_id=str(uuid4()),deadline=time.time()+3600,revision=0)
        db.add(job)
        d.event(db,job,'created',{'message':'任务已入队','actor':user.username,'mode':data.mode})
        return public(job)

@app.get('/api/tasks')
def listing(user=User):
    with d.Session() as db:
        query=select(d.Job).order_by(d.Job.created.desc()).limit(200)
        if user.role!='admin': query=query.where(d.Job.owner_id==user.id)
        return [public(j) for j in db.scalars(query)]

@app.get('/api/tasks/{task_id}')
def detail(task_id:str,user=User):
    with d.Session() as db:
        job=db.get(d.Job,task_id); auth.authorize(user,job)
        return public(job)|{'events':[{'id':e.id,'kind':e.kind,'created':e.created,'payload':json.loads(e.payload)}
            for e in db.scalars(select(d.JobEvent).where(d.JobEvent.job_id==task_id).order_by(d.JobEvent.id))]}

@app.post('/api/tasks/{task_id}/cancel')
def cancel(task_id:str,user=User):
    with d.transaction() as db:
        job=d.lock_job(db,task_id); auth.authorize(user,job,True)
        if job.status in TERMINAL: raise HTTPException(409,'Task already terminal')
        job.cancel_requested=True; job.status='cancelled'; job.approval=None
        d.event(db,job,'cancelled',{'message':'用户取消任务','actor':user.username})
        return public(job)

@app.post('/api/tasks/{task_id}/approval')
def decide(task_id:str,data:Decision,user=User):
    with d.transaction() as db:
        job=d.lock_job(db,task_id); auth.authorize(user,job,True)
        pending=json.loads(job.approval) if job.approval else {}
        if job.status!='waiting_approval' or pending.get('id')!=data.operation_id or pending.get('digest')!=data.digest:
            raise HTTPException(409,'审批已处理或操作已改变')
        job.decision=json.dumps(data.model_dump()|{'reviewer':user.username})
        job.status='queued'; job.approval=None
        d.event(db,job,'decision',{'message':'批准' if data.approve else '拒绝','actor':user.username,'comment':data.comment,'operation_id':data.operation_id})
        return public(job)

@app.get('/api/tasks/{task_id}/events')
async def events(task_id:str,request:Request,after:int=0,user=User):
    detail(task_id,user)
    try: cursor=max(after,int(request.headers.get('last-event-id','0')))
    except ValueError: raise HTTPException(400,'Invalid cursor')
    async def generate():
        nonlocal cursor
        while not await request.is_disconnected():
            # Revocation/logout must also terminate an already-open stream.
            try: auth.current_user(request)
            except HTTPException: return
            with d.Session() as db:
                rows=list(db.scalars(select(d.JobEvent).where(d.JobEvent.job_id==task_id,d.JobEvent.id>cursor).order_by(d.JobEvent.id).limit(100)))
            for row in rows:
                cursor=row.id
                yield f'id: {row.id}\ndata: {json.dumps({"kind":row.kind,"payload":json.loads(row.payload)},ensure_ascii=False)}\n\n'
            if not rows: yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(generate(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

WEB=Path(__file__).parent/'web'
if (WEB/'assets').exists(): app.mount('/assets',StaticFiles(directory=WEB/'assets'),name='assets')
@app.get('/')
def index(): return FileResponse(WEB/'index.html')

class RunbookInput(Input):
    slug:str=Field(pattern=r'^[a-z0-9-]{3,80}$')
    title:str=Field(min_length=3,max_length=200)
    content:str=Field(min_length=20,max_length=50000)
class SearchInput(Input):
    query:str=Field(min_length=1,max_length=2000)
    top_k:int=Field(default=4,ge=1,le=10)
class EvalInput(Input):
    task_ids:list[str]=Field(min_length=1,max_length=30)
    use_judges:bool=False

@app.get('/api/runbooks')
def runbooks(user=User):
    from .knowledge import documents
    return documents()

@app.post('/api/runbooks',status_code=201)
def publish_runbook(data:RunbookInput,user=User):
    if user.role!='admin': raise HTTPException(403,'Admin required')
    from .knowledge import publish
    return {'id':publish(data.slug,data.title,data.content)}

@app.post('/api/knowledge/search')
async def search_knowledge(data:SearchInput,user=User):
    from .mcp_client import call_tool
    try: return await call_tool('search_runbooks',data.model_dump())
    except Exception: raise HTTPException(503,'Knowledge MCP service unavailable') from None

@app.get('/api/integrations')
def integrations(user=User):
    return {'mcp_configured':bool(os.getenv('MCP_URL')),'langfuse_enabled':os.getenv('LANGFUSE_ENABLED')=='true','langfuse_url':os.getenv('LANGFUSE_PUBLIC_URL','http://localhost:3000'),'judges_enabled':os.getenv('ENABLE_JUDGES')=='true'}

@app.post('/api/evaluations',status_code=201)
async def new_evaluation(data:EvalInput,user=User):
    if user.role!='admin': raise HTTPException(403,'Admin required')
    if len(set(data.task_ids))!=len(data.task_ids): raise HTTPException(422,'Duplicate task IDs')
    from .evaluation import evaluate
    entries=[]
    with d.Session() as db:
        for id in data.task_ids:
            job=db.get(d.Job,id); auth.authorize(user,job)
            if job.status not in TERMINAL: raise HTTPException(409,'Evaluate terminal tasks only')
            entries.append((id,json.loads(job.snapshot) if job.snapshot else None))
    try: report=await evaluate(entries,data.use_judges)
    except ValueError as exc: raise HTTPException(409,str(exc)) from None
    id=str(uuid4())
    with d.transaction() as db:
        db.add(d.Evaluation(id=id,owner_id=user.id,created=time.time(),status='completed',report=json.dumps(report,ensure_ascii=False)))
    return {'id':id,**report}

@app.get('/api/evaluations')
def evaluations(user=User):
    if user.role!='admin': raise HTTPException(403,'Admin required')
    with d.Session() as db:
        return [{'id':e.id,'created':e.created,'status':e.status,'report':json.loads(e.report)} for e in db.scalars(select(d.Evaluation).order_by(d.Evaluation.created.desc()).limit(30))]
