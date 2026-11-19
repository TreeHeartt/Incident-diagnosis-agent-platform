import asyncio,unittest
from incident_simulator.evaluation import evaluate,hard_checks
from incident_simulator.observability import redact
from incident_simulator.retrieval import chunks,tokenize
class ExtensionTests(unittest.TestCase):
 def test_missing_and_failed_cases_stay_in_denominator(self):
  r=asyncio.run(evaluate([('a',None),('b',{'status':'failed'})]))
  self.assertEqual(r['denominator'],2)
  self.assertEqual(r['metrics']['completed'],0)
  self.assertIsNone(r['root_cause_accuracy'])
 def test_recovery_without_approval_is_failure(self):
  s={'calls':{'c':{'id':'op','risk':'approval'}},'statuses':{'op':'succeeded'},'events':[{'operation_id':'op','status':'succeeded'}],'report':{'evidence_ids':['op'],'recovery_claim':'recovered'},'environment':{'resolved':True}}
  self.assertFalse(hard_checks(s)['approval_valid'])
 def test_redaction_nested_credentials(self):
  self.assertEqual(redact({'password':'x','items':[{'text':'sk-abcdef'}]})['items'][0]['text'],'[REDACTED]')
 def test_versioned_chunks_and_chinese_tokens(self):
  d={'id':'id','slug':'runbook','title':'Title','version':'a'*64,'content':'x'*1600}
  c=chunks([d]);self.assertEqual(len(c),3)
  self.assertIn('@aaaaaaaaaaaa#0',c[0]['citation'])
  self.assertIn('连接',tokenize('数据库连接池 exhausted'))
