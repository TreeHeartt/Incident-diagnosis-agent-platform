import hashlib
import hmac
import secrets
import time
from uuid import uuid4
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import HTTPException, Request
from sqlalchemy import select
from . import database as d

hasher = PasswordHasher()
DUMMY = hasher.hash(secrets.token_urlsafe(32))
COOKIE = 'incident_session'

def digest(token):
    return hashlib.sha256(token.encode()).hexdigest()

def csrf(token):
    return digest(token + ':csrf')

def create_user(username, password, role='operator'):
    if role not in {'admin', 'operator', 'viewer'} or not 3 <= len(username) <= 80 or not 12 <= len(password) <= 200:
        raise ValueError('Invalid username, password (12–200 chars) or role')
    user = d.User(id=str(uuid4()), username=username, password_hash=hasher.hash(password), role=role, enabled=True)
    with d.transaction() as db:
        if db.scalar(select(d.User).where(d.User.username == username)):
            raise ValueError('Username already exists')
        db.add(user)
    return user

def current_user(request: Request):
    token = request.cookies.get(COOKIE, '')
    with d.Session() as db:
        session = db.get(d.LoginSession, digest(token)) if token else None
        user = db.get(d.User, session.user_id) if session and session.expires > time.time() else None
        if not user or not user.enabled:
            raise HTTPException(401, '请先登录')
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            if not hmac.compare_digest(request.headers.get('x-csrf-token', ''), csrf(token)):
                raise HTTPException(403, 'CSRF validation failed')
        return user

def authorize(user, job, write=False):
    if not job or (user.role != 'admin' and job.owner_id != user.id):
        raise HTTPException(404, 'Task not found')
    if write and user.role not in {'admin', 'operator'}:
        raise HTTPException(403, '需要操作员权限')

def login(username, password):
    with d.Session() as db:
        user = db.scalar(select(d.User).where(d.User.username == username))
    try:
        hasher.verify(user.password_hash if user else DUMMY, password)
    except VerificationError:
        raise HTTPException(401, '用户名或密码错误') from None
    if not user or not user.enabled:
        raise HTTPException(401, '用户名或密码错误')
    token = secrets.token_urlsafe(48)
    with d.transaction() as db:
        db.add(d.LoginSession(token_hash=digest(token), user_id=user.id, expires=time.time()+8*3600))
    return user, token
