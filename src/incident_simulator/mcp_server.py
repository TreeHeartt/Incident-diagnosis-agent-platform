"""Private MCP server: read-only knowledge tools; no route to recovery actions."""
import asyncio
import hmac
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from .retrieval import Retriever
from .knowledge import documents,seed

mcp=FastMCP('Incident Runbooks',host='0.0.0.0',port=8090,stateless_http=True,json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
        allowed_hosts=['mcp:8090','localhost:8090','127.0.0.1:8090'],allowed_origins=[]))
retriever=Retriever()

@mcp.tool()
async def search_runbooks(query:str,top_k:int=4)->dict:
    """Search versioned runbooks with BM25+dense retrieval and neural reranking. Read only."""
    return await asyncio.to_thread(retriever.search,query,top_k)

@mcp.tool()
async def list_runbooks()->list[dict]:
    """List active runbook titles and immutable versions. Read only."""
    return [{k:d[k] for k in ['id','slug','title','version']} for d in documents()]

class AuthMiddleware:
    def __init__(self,app): self.app=app
    async def __call__(self,scope,receive,send):
        if scope['type']=='http':
            token=os.environ.get('MCP_SERVICE_TOKEN','')
            header=dict(scope['headers']).get(b'authorization',b'').decode()
            if not token or not hmac.compare_digest(header,'Bearer '+token):
                return await JSONResponse({'error':'Unauthorized'},status_code=401)(scope,receive,send)
        await self.app(scope,receive,send)

app=AuthMiddleware(mcp.streamable_http_app())
if __name__=='__main__':
    import uvicorn
    seed()
    uvicorn.run(app,host='0.0.0.0',port=8090)
