"""Real LangChain/LangGraph + upstream environment, controlled model only."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import nullcontext
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from incident_simulator.environment import SRESession
from incident_simulator.governance import GovernanceError, Reviewer
from incident_simulator.runtime import IncidentRuntime


class ControlledModel(BaseChatModel):
    actions: list[dict]
    false_claim: bool = False

    @property
    def _llm_type(self):
        return "controlled-integration-test"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        results = [json.loads(m.content) for m in messages if isinstance(m, ToolMessage)]
        rejected = any(r.get("status") in {"rejected", "expired"} for r in results)
        if len(results) < len(self.actions) and not rejected:
            response = AIMessage(content="", tool_calls=[{
                **self.actions[len(results)], "id": f"call-{len(results)}", "type": "tool_call"}])
        else:
            evidence = [r["operation_id"] for r in results if "result" in r]
            response = AIMessage(content=json.dumps({
                "diagnosis": "Auth holds unreleased connections; simulator evidence only.",
                "evidence_ids": evidence,
                "recovery_claim": "recovered" if self.false_claim or not rejected else "not_recovered",
                "remaining_risks": ["No independent probe or recurrence validation"]}))
        return ChatResult(generations=[ChatGeneration(message=response)])


DB_ACTIONS = [
    {"name": "read_logs", "args": {"service": "api"}},
    {"name": "get_metrics", "args": {"target": "database"}},
    {"name": "read_logs", "args": {"service": "auth"}},
    {"name": "apply_fix", "args": {"fix_type": "restart_auth_with_connection_cleanup",
      "reason": "Auth owns 48 leaked connections", "expected_impact": "Release connections"}},
    {"name": "get_service_status", "args": {}},
]


@unittest.skipUnless(os.environ.get("SRE_BENCH_PATH"), "Set SRE_BENCH_PATH for upstream integration tests")
class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def runtime(self, actions=None, **kwargs):
        session = SRESession(Path(os.environ["SRE_BENCH_PATH"]), "db_pool_exhausted")
        return IncidentRuntime(ControlledModel(actions=DB_ACTIONS if actions is None else actions), session, **kwargs)

    async def approve(self, runtime, approve=True):
        op = runtime.pending
        return await runtime.decide(reviewer=Reviewer(subject="test-human", can_approve=True),
                                   operation_id=op.id, expected_digest=op.digest,
                                   approve=approve, comment="Proceed" if approve else "Need another plan")

    async def test_full_approval_recovery_and_export(self):
        runtime = self.runtime()
        result = await runtime.start()
        self.assertEqual(result["status"], "waiting_approval", result)
        self.assertFalse(runtime.session.resolved)
        self.assertEqual(runtime.session.context.environment_version, 3)
        op = runtime.pending
        result = await self.approve(runtime)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertTrue(runtime.session.resolved)
        self.assertEqual(runtime.session.context.environment_version, 4)
        with self.assertRaises(GovernanceError):
            await runtime.decide(reviewer=Reviewer(subject="test-human", can_approve=True),
                                 operation_id=op.id, expected_digest=op.digest, approve=True)
        self.assertEqual(runtime.session.context.environment_version, 4)
        directory_context = (nullcontext(os.environ["TEST_OUTPUT_DIR"]) if os.environ.get("TEST_OUTPUT_DIR")
                             else tempfile.TemporaryDirectory())
        with directory_context as directory:
            path = Path(directory) / f"trace-{uuid4()}.json"
            await runtime.export(path)
            trace = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(trace["verification"]["independent_probe"], "not_available")
            self.assertTrue(trace["events"])
            self.assertNotIn('"root_cause":', json.dumps(trace))
            path.unlink()

    async def test_other_scenarios_and_each_write_needs_approval(self):
        recovery = {"reason": "Evidence from logs", "expected_impact": "Restore simulated service"}
        scenarios = {
            "disk_full": [
                {"name": "get_metrics", "args": {"target": "disk"}},
                {"name": "read_logs", "args": {"service": "app"}},
                {"name": "apply_fix", "args": {**recovery, "fix_type": "delete_log",
                  "target": "/var/log/app/app.log.2024-01-14"}},
                {"name": "get_service_status", "args": {}},
            ],
            "data_corruption": [
                *[{"name": "read_logs", "args": {"service": s}} for s in ["orders", "fulfillment", "finance"]],
                {"name": "run_diagnostic", "args": {"type": "deploy_history"}},
                {"name": "rollback_service", "args": {**recovery, "service": "orders"}},
                {"name": "apply_fix", "args": {**recovery, "fix_type": "reprocess_affected_orders"}},
                {"name": "get_service_status", "args": {}},
            ],
        }
        for scenario, actions in scenarios.items():
            with self.subTest(scenario=scenario):
                runtime = IncidentRuntime(ControlledModel(actions=actions),
                    SRESession(Path(os.environ["SRE_BENCH_PATH"]), scenario))
                result = await runtime.start()
                approval_ids = []
                while result["status"] == "waiting_approval":
                    approval_ids.append(runtime.pending.id)
                    result = await self.approve(runtime)
                self.assertEqual(result["status"], "succeeded", result)
                self.assertEqual(len(set(approval_ids)), 2 if scenario == "data_corruption" else 1)

    async def test_rejection_never_executes_recovery(self):
        runtime = self.runtime()
        await runtime.start()
        result = await self.approve(runtime, False)
        self.assertEqual(result["status"], "incomplete", result)
        self.assertFalse(runtime.session.resolved)
        self.assertEqual(runtime.session.context.environment_version, 3)

    async def test_forged_approval_is_rejected(self):
        runtime = self.runtime()
        await runtime.start()
        with self.assertRaises(GovernanceError):
            await runtime.decide(reviewer=Reviewer(subject="reader"), operation_id=runtime.pending.id,
                                 expected_digest=runtime.pending.digest, approve=True)
        self.assertEqual(runtime.status, "waiting_approval")
        self.assertFalse(runtime.session.resolved)

    async def test_budget_is_enforced_across_model_turns(self):
        runtime = self.runtime(max_tool_calls=2)
        result = await runtime.start()
        self.assertEqual(result["status"], "failed")
        self.assertEqual(runtime.session.context.environment_version, 2)

    async def test_false_success_report_fails(self):
        runtime = self.runtime(actions=DB_ACTIONS[:1])
        result = await runtime.start()
        self.assertEqual(result["status"], "failed")
        self.assertFalse(runtime.session.resolved)

    async def test_sessions_are_isolated(self):
        first, second = self.runtime(), self.runtime()
        await first.start()
        await self.approve(first)
        self.assertTrue(first.session.resolved)
        self.assertFalse(second.session.resolved)
        self.assertEqual(second.session.context.environment_version, 0)


if __name__ == "__main__":
    unittest.main()
