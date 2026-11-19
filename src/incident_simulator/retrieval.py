"""Local neural embeddings + BM25 -> reciprocal rank fusion -> neural reranker."""
import hashlib
import os
import re
import threading
import time
from uuid import uuid5,NAMESPACE_URL
from qdrant_client import QdrantClient, models
from rank_bm25 import BM25Okapi
from .knowledge import documents


def tokenize(text):
    text=text.lower()
    words=re.findall(r'[a-z0-9_]+',text)
    for block in re.findall(r'[\u4e00-\u9fff]+',text):
        words.extend(block[i:i+2] for i in range(max(1,len(block)-1)))
    return words or ['_empty_']

def chunks(docs):
    output=[]
    for doc in docs:
        for n,start in enumerate(range(0,len(doc['content']),750)):
            content=doc['content'][start:start+900]
            citation=f"{doc['slug']}@{doc['version'][:12]}#{n}"
            output.append(dict(citation=citation,document_id=doc['id'],title=doc['title'],content=content,version=doc['version']))
    return output

class Retriever:
    def __init__(self,client=None,embedder=None,reranker=None):
        self.client=client or QdrantClient(url=os.getenv('QDRANT_URL','http://qdrant:6333'),timeout=30)
        self.embedder=embedder; self.reranker=reranker
        self.lock=threading.RLock(); self.revision=None; self.items=[]
        self.embedding_name=os.getenv('EMBEDDING_MODEL','sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2')
        self.reranker_name=os.getenv('RERANKER_MODEL','incident/jina-v2-multilingual-int8')
    def load_models(self):
        if self.embedder is None:
            from fastembed import TextEmbedding
            self.embedder=TextEmbedding(self.embedding_name,cache_dir=os.getenv('MODEL_CACHE','/models'),threads=2,local_files_only=os.getenv('MODEL_OFFLINE')=='true')
        if self.reranker is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
            from fastembed.common.model_description import ModelSource
            if self.reranker_name=='incident/jina-v2-multilingual-int8' and not any(m['model']==self.reranker_name for m in TextCrossEncoder.list_supported_models()):
                TextCrossEncoder.add_custom_model(self.reranker_name,ModelSource(hf='jinaai/jina-reranker-v2-base-multilingual'),model_file='onnx/model_int8.onnx',license='cc-by-nc-4.0',size_in_gb=0.3)
            self.reranker=TextCrossEncoder(self.reranker_name,cache_dir=os.getenv('MODEL_CACHE','/models'),threads=2,local_files_only=os.getenv('MODEL_OFFLINE')=='true')
    def refresh(self):
        docs=documents()
        rev=hashlib.sha256((self.embedding_name+str([(d['id'],d['version']) for d in docs])).encode()).hexdigest()[:20]
        if rev==self.revision: return
        self.load_models()
        items=chunks(docs)
        if not items: raise ValueError('Knowledge base is empty')
        collection='incident_runbooks_'+rev
        exists=self.client.collection_exists(collection)
        if not exists or self.client.count(collection,exact=True).count!=len(items):
            vectors=list(self.embedder.embed([x['title']+'\n'+x['content'] for x in items]))
            if not exists: self.client.create_collection(collection,models.VectorParams(size=len(vectors[0]),distance=models.Distance.COSINE))
            self.client.upsert(collection,[models.PointStruct(id=str(uuid5(NAMESPACE_URL,x['citation'])),vector=v.tolist(),payload=x) for x,v in zip(items,vectors)],wait=True)
        self.items=items; self.collection=collection
        self.bm25=BM25Okapi([tokenize(x['title']+' '+x['content']) for x in items])
        self.revision=rev
    def search(self,query,top_k=4):
        if not query.strip() or len(query)>2000 or not 1<=top_k<=10: raise ValueError('Invalid search arguments')
        start=time.monotonic()
        with self.lock:
            self.refresh()
            vector=list(self.embedder.embed([query]))[0].tolist()
            dense=self.client.query_points(self.collection,query=vector,limit=20,with_payload=True).points
            scores=self.bm25.get_scores(tokenize(query))
            lexical=sorted(range(len(self.items)),key=lambda i:float(scores[i]),reverse=True)[:20]
            fused={}; payloads={x['citation']:x for x in self.items}
            channels={}
            for name,ids in [('dense',[p.payload['citation'] for p in dense]),('bm25',[self.items[i]['citation'] for i in lexical if scores[i]>0])]:
                for rank,citation in enumerate(ids,1):
                    fused[citation]=fused.get(citation,0)+1/(60+rank)
                    channels.setdefault(citation,[]).append(name)
            candidates=sorted(fused,key=fused.get,reverse=True)[:12]
            reranked=list(self.reranker.rerank(query,[payloads[c]['title']+'\n'+payloads[c]['content'] for c in candidates]))
            order=sorted(range(len(candidates)),key=lambda i:float(reranked[i]),reverse=True)[:top_k]
            hits=[payloads[candidates[i]]|{'rrf_score':fused[candidates[i]],'rerank_score':float(reranked[i]),'channels':channels[candidates[i]]} for i in order]
            return {'query':query,'index_revision':self.revision,'embedding_model':self.embedding_name,'reranker_model':self.reranker_name,'latency_ms':round((time.monotonic()-start)*1000),'hits':hits,'notice':'Reference material only; never an authorization to execute.'}
