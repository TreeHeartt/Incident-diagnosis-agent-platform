"""Terminal entry point for the same runtime later used by FastAPI workers."""
import argparse
import asyncio
import json
from pathlib import Path
from .config import Settings
from .environment import SRESession
from .governance import Reviewer
from .runtime import IncidentRuntime


async def run(args):
    settings = Settings.from_env()
    session = SRESession(settings.sre_bench_path, args.scenario)
    runtime = IncidentRuntime(settings.build_model(), session)
    output = args.output / f"{session.context.task_id}.json"
    try:
        snapshot = await runtime.start()
        await runtime.export(output)
        while snapshot["status"] == "waiting_approval":
            print(json.dumps(snapshot["approval"], ensure_ascii=False, indent=2))
            decision = (await asyncio.to_thread(input, "批准本次操作？输入 approve 或 reject：")).strip()
            if decision not in {"approve", "reject"}:
                continue
            comment = await asyncio.to_thread(input, "审批意见（可留空）：")
            op = runtime.pending
            snapshot = await runtime.decide(reviewer=Reviewer(subject="local-terminal-operator", can_approve=True),
                      operation_id=op.id, expected_digest=op.digest, approve=decision == "approve", comment=comment)
            await runtime.export(output)
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        print(f"轨迹：{output.resolve()}")
        return 0 if snapshot["status"] == "succeeded" else 1
    finally:
        await runtime.export(output)


def main():
    parser = argparse.ArgumentParser(description="Run a governed incident task")
    parser.add_argument("--scenario", choices=["db_pool_exhausted", "disk_full", "data_corruption"],
                        default="db_pool_exhausted")
    parser.add_argument("--output", type=Path, default=Path("runs"))
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except ValueError as exc:
        # Settings errors contain names, not secret values.
        print("Configuration error: " + str(exc) if "Missing environment variables" in str(exc)
              else "Configuration or environment error; check model URL and SRE_BENCH_PATH.")
        raise SystemExit(2)


if __name__ == "__main__":
    main()
