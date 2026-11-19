import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
import unittest
from uuid import uuid4
from unittest.mock import patch
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from incident_simulator import database as d, api, auth, worker

@unittest.skipUnless(os.environ.get('SRE_BENCH_PATH'), 'Set SRE_BENCH_PATH')
class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.directory=Path(os.environ['TEST_OUTPUT_DIR'])
        self.path=self.directory / f'platform-{uuid4()}.db'
        self.checkpoint=self.directory / f'checkpoint-{uuid4()}.db'
        engine=create_engine('sqlite:///'+self.path.as_posix(),connect_args={'check_same_thread':False})
        self.patches=[patch.object(d,'engine',engine),patch.object(d,'Session',sessionmaker(engine,expire_on_commit=False)),patch.object(d,'URL','sqlite://'),patch.dict(os.environ,{'CHECKPOINT_SQLITE_PATH':str(self.checkpoint),'ENABLE_CLOUD_MODEL':'false'})]
        for p in self.patches: p.start()
        d.initialize()
        api.login_attempts.clear()
        auth.create_user('admin','test-password-123','admin')
        self.client=TestClient(api.app)
        self.login('admin')

    def tearDown(self):
        self.client.close()
        d.engine.dispose()
        for p in reversed(self.patches): p.stop()
        for path in [self.path,self.checkpoint]:
            if path.exists(): path.unlink()

    def login(self,name):
        response=self.client.post('/api/auth/login',json={'username':name,'password':'test-password-123'})
        self.assertEqual(response.status_code,200,response.text)
        self.client.headers['x-csrf-token']=response.json()['csrf']

    def create(self,scenario='db_pool_exhausted'):
        response=self.client.post('/api/tasks',json={'scenario':scenario})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()['id']

    def tick(self):
        # Close and reopen both the runtime and durable graph saver each time.
        asyncio.run(worker.main(once=True))

    def detail(self,id): return self.client.get('/api/tasks/'+id).json()
    def decide(self,id,approve=True):
        op=self.detail(id)['approval']
        payload={'operation_id':op['id'],'digest':op['digest'],'approve':approve,'comment':'integration test'}
        response=self.client.post(f'/api/tasks/{id}/approval',json=payload)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.client.post(f'/api/tasks/{id}/approval',json=payload).status_code,409)

    def test_all_scenarios_survive_worker_restart(self):
        for scenario in api.SCENARIOS:
            id=self.create(scenario)
            self.tick()
            decisions=0
            while self.detail(id)['status']=='waiting_approval':
                self.decide(id); self.tick(); decisions+=1
                self.assertLess(decisions,4)
            result=self.detail(id)
            self.assertEqual(result['status'],'succeeded',result)
            self.assertEqual(decisions,2 if scenario=='data_corruption' else 1)
            self.assertTrue(result['report']['verification']['environment_resolved'])
            self.assertNotIn('"root_cause":',json.dumps(result))
            completed=[e['payload']['operation_id'] for e in result['events'] if e['kind']=='tool' and e['payload']['status']=='succeeded']
            self.assertEqual(len(completed),len(set(completed)))

    def test_auth_permissions_csrf_and_ownership(self):
        id=self.create()
        self.client.headers.pop('x-csrf-token')
        self.assertEqual(self.client.post('/api/tasks',json={'scenario':'disk_full'}).status_code,403)
        for name,role in [('other','operator'),('reader','viewer')]:
            auth.create_user(name,'test-password-123',role)
            self.login(name)
            self.assertEqual(self.client.get('/api/tasks/'+id).status_code,404)
            self.assertEqual(self.client.get('/api/users').status_code,403)
        self.assertEqual(self.client.post('/api/tasks',json={'scenario':'disk_full'}).status_code,403)
        self.assertEqual(self.client.post('/api/auth/logout').status_code,200)
        self.assertEqual(self.client.get('/api/tasks').status_code,401)

    def test_rejection_cancel_and_cloud_disabled(self):
        self.assertEqual(self.client.post('/api/tasks',json={'scenario':'disk_full','mode':'cloud'}).status_code,409)
        id=self.create(); self.tick(); self.decide(id,False); self.tick()
        self.assertEqual(self.detail(id)['status'],'incomplete')
        id=self.create()
        self.assertEqual(self.client.post(f'/api/tasks/{id}/cancel').status_code,200)
        self.tick()
        self.assertEqual(self.detail(id)['status'],'cancelled')

    def test_uncertain_execution_is_not_retried(self):
        id=self.create(); self.tick()
        with d.transaction() as db:
            job=d.lock_job(db,id)
            state=json.loads(job.snapshot)
            state['statuses'][state['pending']]='running'
            job.snapshot=json.dumps(state); job.status='queued'
        self.tick()
        self.assertEqual(self.detail(id)['status'],'needs_review')

    def test_separate_worker_process_resumes_checkpoint(self):
        id=self.create('data_corruption')
        env=os.environ.copy()
        env['DATABASE_URL']='sqlite:///'+self.path.as_posix()
        code='import asyncio; from incident_simulator.worker import main; asyncio.run(main(once=True))'
        for iteration in range(3):
            run=subprocess.run([sys.executable,'-c',code],env=env,capture_output=True,timeout=40)
            self.assertEqual(run.returncode,0,run.stderr.decode(errors='replace'))
            result=self.detail(id)
            if iteration<2:
                self.assertEqual(result['status'],'waiting_approval',result)
                self.decide(id)
            else:
                self.assertEqual(result['status'],'succeeded',result)

    def test_previous_decision_does_not_approve_next_interrupt(self):
        id=self.create('data_corruption'); self.tick(); self.decide(id)
        with d.Session() as db: previous=db.get(d.Job,id).decision
        self.tick()
        pending=self.detail(id)['approval']['id']
        # Model the crash window after saving the next interrupt, before finalizing the job.
        with d.transaction() as db:
            job=d.lock_job(db,id); job.decision=previous; job.status='queued'
        self.tick()
        self.assertEqual(self.detail(id)['status'],'waiting_approval')
        self.assertEqual(self.detail(id)['approval']['id'],pending)
        self.decide(id); self.tick()
        self.assertEqual(self.detail(id)['status'],'succeeded')

    def test_waiting_approval_obeys_task_deadline(self):
        id=self.create(); self.tick()
        with d.transaction() as db: d.lock_job(db,id).deadline=0
        self.tick()
        self.assertEqual(self.detail(id)['status'],'timed_out')

    def test_sse_cursor_and_logout_revocation(self):
        id=self.create(); self.tick()
        first=self.detail(id)['events'][0]['id']
        token=self.client.cookies.get(auth.COOKIE)
        class Request:
            headers={'last-event-id':str(first)}
            cookies={auth.COOKIE:token}
            method='GET'
            async def is_disconnected(self): return False
        async def read():
            user=auth.current_user(Request())
            response=await api.events(id,Request(),user=user)
            message=await response.body_iterator.__anext__()
            self.assertNotIn(f'id: {first}\n',message)
            self.assertIn('claimed',message)
            await response.body_iterator.aclose()
            self.client.post('/api/auth/logout')
            response=await api.events(id,Request(),user=user)
            with self.assertRaises(StopAsyncIteration): await response.body_iterator.__anext__()
        asyncio.run(read())
