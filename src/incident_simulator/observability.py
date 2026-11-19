"""Local Langfuse tracing. Secret redaction is applied before exporting observations."""
import os
import re
from contextlib import nullcontext
from functools import lru_cache

def redact(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if any(s in k.lower() for s in ['password','api_key','authorization','secret','token']) else redact(v)) for k,v in value.items()}
    if isinstance(value,list): return [redact(x) for x in value]
    if isinstance(value,str): return re.sub(r'sk-[A-Za-z0-9_-]+','[REDACTED]',value)
    return value

@lru_cache
def client():
    if os.getenv('LANGFUSE_ENABLED')!='true': return None
    from langfuse import Langfuse
    return Langfuse(base_url=os.environ['LANGFUSE_BASE_URL'],public_key=os.environ['LANGFUSE_PUBLIC_KEY'],secret_key=os.environ['LANGFUSE_SECRET_KEY'],mask=lambda **kw:redact(kw.get('data')),flush_interval=1,environment='local')

def callbacks(trace_id):
    if not client(): return []
    from langfuse.langchain import CallbackHandler
    return [CallbackHandler(public_key=os.environ['LANGFUSE_PUBLIC_KEY'],trace_context={'trace_id':trace_id.replace('-','')})]

def span(trace_id,name,kind='span',**kwargs):
    lf=client()
    return lf.start_as_current_observation(trace_context={'trace_id':trace_id.replace('-','')},name=name,as_type=kind,**kwargs) if lf else nullcontext()

def url(trace_id):
    if os.getenv('LANGFUSE_ENABLED')!='true': return None
    return os.getenv('LANGFUSE_PUBLIC_URL','http://localhost:3000')+'/project/'+os.getenv('LANGFUSE_PROJECT_ID','incident-project')+'/traces/'+trace_id.replace('-','')
