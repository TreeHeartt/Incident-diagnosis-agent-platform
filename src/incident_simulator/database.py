"""Durable application data. Schema is versioned by Alembic."""
import os
import time
import json
from contextlib import contextmanager
from sqlalchemy import create_engine, Column, String, Text, Integer, Float, Boolean, select
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()
URL = os.getenv('DATABASE_URL', 'sqlite:///incident.db')
engine = create_engine(URL, pool_pre_ping=True, connect_args={'check_same_thread': False, 'timeout': 30} if URL.startswith('sqlite') else {})
Session = sessionmaker(engine, expire_on_commit=False)

class User(Base):
    __tablename__ = 'users'
    id = Column(String, primary_key=True)
    username = Column(String(80), unique=True, nullable=False)
    password_hash = Column(Text, nullable=False)
    role = Column(String(20), nullable=False)
    enabled = Column(Boolean, default=True)

class LoginSession(Base):
    __tablename__ = 'login_sessions'
    token_hash = Column(String(64), primary_key=True)
    user_id = Column(String, nullable=False, index=True)
    expires = Column(Float, nullable=False)

class Job(Base):
    __tablename__ = 'jobs'
    id = Column(String, primary_key=True)
    owner_id = Column(String, nullable=False, index=True)
    scenario = Column(String, nullable=False)
    mode = Column(String, nullable=False)
    status = Column(String, nullable=False, index=True)
    created = Column(Float, nullable=False)
    updated = Column(Float, nullable=False)
    trace_id = Column(String, nullable=False)
    snapshot = Column(Text)
    approval = Column(Text)
    decision = Column(Text)
    report = Column(Text)
    error = Column(String)
    lease_owner = Column(String)
    lease_until = Column(Float, default=0)
    cancel_requested = Column(Boolean, default=False)
    deadline = Column(Float, nullable=False)
    revision = Column(Integer, default=0)

class JobEvent(Base):
    __tablename__ = 'job_events'
    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(String, nullable=False, index=True)
    kind = Column(String, nullable=False)
    payload = Column(Text, nullable=False)
    created = Column(Float, nullable=False)

class WorkerHeartbeat(Base):
    __tablename__ = 'workers'
    id = Column(String, primary_key=True)
    seen = Column(Float, nullable=False)

@contextmanager
def transaction():
    with Session.begin() as db:
        if engine.dialect.name == 'sqlite':
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        yield db

def event(db, job, kind, payload):
    job.updated = time.time()
    job.revision += 1
    db.add(JobEvent(job_id=job.id, kind=kind, payload=json.dumps(payload, ensure_ascii=False), created=job.updated))

def lock_job(db, job_id):
    return db.scalar(select(Job).where(Job.id == job_id).with_for_update())

def initialize():
    # Used by tests and bootstrap only; normal deployments run Alembic upgrade head.
    Base.metadata.create_all(engine)

class Runbook(Base):
    __tablename__='runbooks'
    id=Column(String,primary_key=True)
    slug=Column(String,nullable=False,index=True)
    title=Column(String,nullable=False)
    content=Column(Text,nullable=False)
    version=Column(String,nullable=False)
    active=Column(Boolean,nullable=False,default=True)
    created=Column(Float,nullable=False)

class Evaluation(Base):
    __tablename__='evaluations'
    id=Column(String,primary_key=True)
    owner_id=Column(String,nullable=False,index=True)
    created=Column(Float,nullable=False)
    status=Column(String,nullable=False)
    report=Column(Text,nullable=False)
