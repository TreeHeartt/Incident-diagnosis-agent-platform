"""Lease-fenced worker using the actual LangChain runtime and durable checkpointers."""
import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4, UUID
from sqlalchemy import select, or_, and_
from . import database as d
from .runtime import IncidentRuntime
from .environment import SRESession
from .fixture_model import FixtureModel
from .config import Settings
from .governance import Reviewer
from langgraph.types import Command

LEASE_SECONDS=30
TERMINAL={'succeeded','failed','incomplete','cancelled','timed_out','needs_review'}
class LeaseLost(Exception): pass

@asynccontextmanager
async def saver():
    if d.URL.startswith('postgresql'):
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        connection=d.URL.replace('postgresql+psycopg://','postgresql://')
        async with AsyncPostgresSaver.from_conn_string(connection) as checkpointer:
            await checkpointer.setup()
            yield checkpointer
    else:
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        path=os.getenv('CHECKPOINT_SQLITE_PATH','checkpoints.db')
        async with AsyncSqliteSaver.from_conn_string(path) as checkpointer:
            await checkpointer.setup()
            yield checkpointer

def claim(worker_id):
    now=time.time()
    with d.transaction() as db:
        db.merge(d.WorkerHeartbeat(id=worker_id,seen=now))
        job=db.scalar(select(d.Job).where(or_(d.Job.status.in_(['queued','running']),
            and_(d.Job.status=='waiting_approval',d.Job.deadline<now)),
            or_(d.Job.lease_until==None,d.Job.lease_until<now)).order_by(d.Job.created).with_for_update(skip_locked=True).limit(1))
        if not job: return None
        if job.cancel_requested or job.deadline<now:
            job.status='cancelled' if job.cancel_requested else 'timed_out'
            d.event(db,job,job.status,{'message':'任务已取消或超过截止时间'})
            return None
        job.lease_owner=worker_id; job.lease_until=now+LEASE_SECONDS; job.status='running'
        d.event(db,job,'claimed',{'message':'Worker 领取任务','worker':worker_id})
        return job.id

async def execute(job_id,worker_id,checkpointer):
    with d.Session() as db:
        job=db.get(d.Job,job_id)
        saved=json.loads(job.snapshot) if job.snapshot else None
        decision=json.loads(job.decision) if job.decision else None
    try:
        session=SRESession(Path(os.environ['SRE_BENCH_PATH']),job.scenario)
        session.context=session.context.model_copy(update={'task_id':UUID(job.id),'trace_id':UUID(job.trace_id)})
        if job.mode=='cloud':
            if os.getenv('ENABLE_CLOUD_MODEL')!='true': raise ValueError('Cloud disabled')
            model=Settings.from_env().build_model()
        else: model=FixtureModel(scenario=job.scenario)
        runtime=IncidentRuntime(model,session,checkpointer=checkpointer)
        if saved: runtime.restore(saved)
        async def persist():
            with d.transaction() as db:
                row=d.lock_job(db,job_id)
                if row.lease_owner!=worker_id or row.cancel_requested or row.lease_until<time.time(): raise LeaseLost()
                old=json.loads(row.snapshot) if row.snapshot else {}
                row.snapshot=json.dumps(runtime.dump_state(),ensure_ascii=False)
                # Emit public operation events only, never the private environment snapshot.
                for event in runtime.model_events[len(old.get('model_events',[])):]:
                    d.event(db,row,'model',event|{'message':'模型调用完成','trace_id':row.trace_id})
                old_events=len(old.get('events',[]))
                for e in runtime.gate.events()[old_events:]:
                    op=runtime.gate._operations[e.operation_id]
                    d.event(db,row,'tool',{'message':f'{op.tool}: {e.status.value}','operation_id':str(op.id),'tool':op.tool,'status':e.status.value,'actor':e.actor})
        runtime.persist=persist; runtime.gate.persist=persist
        async def run():
            if runtime.status=='needs_review': return
            if decision:
                if runtime.pending:
                    if str(runtime.pending.id) == decision['operation_id']:
                        runtime.status='waiting_approval'
                        await runtime.decide(reviewer=Reviewer(subject=decision['reviewer'],can_approve=True),
                            operation_id=UUID(decision['operation_id']),expected_digest=decision['digest'],
                            approve=decision['approve'],comment=decision['comment'])
                    else:
                        # Crash after reaching the next approval: do not reuse an earlier decision.
                        await runtime.continue_run()
                else:
                    graph_state = await runtime.agent.aget_state(runtime.config)
                    if any(t.interrupts for t in graph_state.tasks):
                        await runtime._invoke(Command(resume=True))
                    else:
                        await runtime.continue_run()
            elif saved: await runtime.continue_run()
            else: await runtime.start()
        execution=asyncio.create_task(run())
        try:
            while not execution.done():
                await asyncio.wait({execution},timeout=5)
                if execution.done(): break
                with d.transaction() as db:
                    row=d.lock_job(db,job_id)
                    if row.lease_owner!=worker_id or row.cancel_requested or row.deadline<time.time():
                        execution.cancel()
                    else:
                        row.lease_until=time.time()+LEASE_SECONDS
                        db.merge(d.WorkerHeartbeat(id=worker_id,seen=time.time()))
            await execution
        finally:
            if not execution.done():
                execution.cancel()
                await asyncio.gather(execution,return_exceptions=True)
        with d.transaction() as db:
            row=d.lock_job(db,job_id)
            if row.lease_owner!=worker_id or row.cancel_requested: return
            row.snapshot=json.dumps(runtime.dump_state(),ensure_ascii=False)
            row.status=runtime.status
            row.approval=json.dumps(runtime.pending.model_dump(mode='json'),ensure_ascii=False) if runtime.pending else None
            row.report=json.dumps(runtime.snapshot(),ensure_ascii=False) if runtime.status in TERMINAL else None
            row.error=runtime.error; row.decision=None; row.lease_owner=None; row.lease_until=0
            d.event(db,row,runtime.status,{'message':'等待逐次审批' if runtime.pending else '执行片段已完成','model':runtime.model_name})
    except (Exception,asyncio.CancelledError) as exc:
        with d.transaction() as db:
            row=d.lock_job(db,job_id)
            if row.lease_owner==worker_id:
                if not row.cancel_requested:
                    row.status='timed_out' if row.deadline<time.time() else 'failed'
                    row.error=type(exc).__name__
                    d.event(db,row,row.status,{'message':'执行失败','error_type':type(exc).__name__})
                row.lease_until=0; row.lease_owner=None

async def main(once=False):
    worker_id=str(uuid4())
    async with saver() as checkpointer:
        while True:
            task=claim(worker_id)
            if task: await execute(task,worker_id,checkpointer)
            if once: return
            await asyncio.sleep(1)

if __name__=='__main__':
    if os.name=='nt': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
