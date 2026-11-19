import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from pydantic import ValidationError
from incident_simulator.governance import (
    Context, Contract, ExecutionGate, GovernanceError, Reviewer, Risk, Status,
    ToolDefinition, ToolRegistry,
)


class ServiceArgs(Contract):
    service: str


class GovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = 0
        self.now = datetime(2026, 11, 17, tzinfo=timezone.utc)
        self.context = Context(task_id=uuid4(), environment_id=uuid4(),
                               environment_version=0, trace_id=uuid4())
        self.registry = ToolRegistry()
        async def handler(args, context):
            self.calls += 1
            await asyncio.sleep(0)
            return {"service": args.service, "executed": True}
        self.registry.register(ToolDefinition(name="restart", description="Restart service",
                                              risk=Risk.APPROVAL), ServiceArgs, handler)
        self.registry.register(ToolDefinition(name="logs", description="Read logs",
                                              risk=Risk.READ), ServiceArgs, handler)
        self.gate = ExecutionGate(self.registry, clock=lambda: self.now)
        self.reviewer = Reviewer(subject="operator", can_approve=True)

    async def propose(self, tool="restart", arguments=None):
        return await self.gate.propose(self.context, tool, arguments or {"service": "auth"},
                                       reason="Connection evidence", expected_impact="Release connections")

    async def approve(self, op):
        await self.gate.decide(op.id, reviewer=self.reviewer, approve=True, expected_digest=op.digest)

    async def test_write_requires_exact_approval_and_is_not_reexecuted(self):
        op = await self.propose()
        with self.assertRaises(GovernanceError):
            await self.gate.execute(op.id, current_context=self.context)
        with self.assertRaises(GovernanceError):
            await self.gate.decide(op.id, reviewer=self.reviewer, approve=True, expected_digest="wrong")
        await self.approve(op)
        result = await self.gate.execute(op.id, current_context=self.context)
        result["service"] = "tampered"
        repeated = await self.gate.execute(op.id, current_context=self.context)
        self.assertEqual(repeated["service"], "auth")
        self.assertEqual(self.calls, 1)
        other = await self.propose()
        with self.assertRaises(GovernanceError):
            await self.gate.execute(other.id, current_context=self.context)

    async def test_reads_run_without_approval_and_parameters_are_validated(self):
        with self.assertRaises(ValidationError):
            await self.propose(arguments={"service": "auth", "approved": True})
        op = await self.propose("logs")
        await self.gate.execute(op.id, current_context=self.context)
        self.assertEqual(self.calls, 1)

    async def test_rejection_and_unprivileged_approval(self):
        op = await self.propose()
        with self.assertRaises(GovernanceError):
            await self.gate.decide(op.id, reviewer=Reviewer(subject="reader"),
                                   approve=True, expected_digest=op.digest)
        await self.gate.decide(op.id, reviewer=self.reviewer, approve=False,
                               expected_digest=op.digest, comment="Need more evidence")
        with self.assertRaises(GovernanceError):
            await self.gate.execute(op.id, current_context=self.context)
        self.assertEqual(self.calls, 0)

    async def test_expiry_and_changed_environment(self):
        op = await self.propose()
        await self.approve(op)
        with self.assertRaises(GovernanceError):
            await self.gate.execute(op.id, current_context=self.context.model_copy(update={"environment_version": 1}))
        self.now += timedelta(hours=1)
        with self.assertRaises(GovernanceError):
            await self.gate.execute(op.id, current_context=self.context)
        self.assertEqual(self.gate.status(op.id), Status.EXPIRED)
        self.assertEqual(self.calls, 0)

    async def test_concurrent_execution_does_not_repeat_effect(self):
        op = await self.propose()
        await self.approve(op)
        await asyncio.gather(*(self.gate.execute(op.id, current_context=self.context)
                               for _ in range(2)), return_exceptions=True)
        self.assertEqual(self.calls, 1)
        self.assertEqual(self.gate.status(op.id), Status.SUCCEEDED)

    async def test_uncertain_write_cannot_be_blindly_retried(self):
        async def failing_handler(args, context):
            self.calls += 1
            raise TimeoutError("Response lost after possible execution")
        self.registry.register(ToolDefinition(name="repair", description="Repair", risk=Risk.APPROVAL),
                               ServiceArgs, failing_handler)
        op = await self.propose("repair")
        await self.approve(op)
        with self.assertRaises(TimeoutError):
            await self.gate.execute(op.id, current_context=self.context)
        self.assertEqual(self.gate.status(op.id), Status.UNKNOWN)
        with self.assertRaises(GovernanceError):
            await self.gate.execute(op.id, current_context=self.context)
        self.assertEqual(self.calls, 1)


if __name__ == "__main__":
    unittest.main()
