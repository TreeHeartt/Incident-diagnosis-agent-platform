"""Offline model substitute; exercises the production harness and environment."""
import json
import os
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

class FixtureModel(BaseChatModel):
    scenario: str
    @property
    def _llm_type(self): return 'offline-fixture'
    def bind_tools(self,tools,**kwargs): return self
    def _generate(self,messages,stop=None,run_manager=None,**kwargs):
        reason={'reason':'离线测试模型根据场景证据提出操作','expected_impact':'操作仅作用于 SRE-Bench 模拟环境'}
        def call(name,**args): return {'name':name,'args':args}
        actions={
            'db_pool_exhausted':[call('read_logs',service='api'),call('get_metrics',target='database'),call('read_logs',service='auth'),call('apply_fix',fix_type='restart_auth_with_connection_cleanup',**reason)],
            'disk_full':[call('get_metrics',target='disk'),call('read_logs',service='app'),call('apply_fix',fix_type='delete_log',target='/var/log/app/app.log.2024-01-14',**reason)],
            'data_corruption':[call('read_logs',service=s) for s in ['orders','fulfillment','finance']]+[call('run_diagnostic',type='deploy_history'),call('rollback_service',service='orders',**reason),call('apply_fix',fix_type='reprocess_affected_orders',**reason)]
        }[self.scenario]+[call('get_service_status')]
        if os.getenv('MCP_URL'): actions.insert(0,call('search_runbooks',query={'db_pool_exhausted':'database connection pool exhausted auth connections','disk_full':'disk full log rotation no space','data_corruption':'orders data corruption deployment rollback reprocess'}[self.scenario]))
        results=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        rejected=any(r.get('status') in {'rejected','expired'} for r in results)
        if len(results)<len(actions) and not rejected:
            answer=AIMessage(content='',tool_calls=[actions[len(results)]|{'id':f'fixture-{len(results)}','type':'tool_call'}])
        else:
            answer=AIMessage(content=json.dumps({'diagnosis':'离线测试模型的诊断结果；不代表真实模型能力',
                'evidence_ids':[r['operation_id'] for r in results if 'result' in r],
                'recovery_claim':'not_recovered' if rejected else 'recovered',
                'remaining_risks':['模拟环境终态，不是独立恢复探针','使用离线测试模型，无付费调用']},ensure_ascii=False))
        return ChatResult(generations=[ChatGeneration(message=answer)])
