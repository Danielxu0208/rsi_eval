"""Stop the upstream driver on exhausted authentication or subscription quota."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def stop_run(run_dir: Path, reason: str, source: str) -> None:
    """Use RRSI's own STOP file and bypass its ordinary candidate-error handler.

    SystemExit escapes loop._evaluate's ``except Exception``. Its driver sees
    STOP before starting the next round; completed frontier/history stay intact.
    """
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    record = {"reason": reason, "source": source,
              "recorded_at": datetime.now(timezone.utc).isoformat(),
              "resume": "Resolve authentication or quota, then use the explicit resume command for the same run."}
    (run_dir / "fatal_error.json").write_text(json.dumps(record, indent=2) + "\n")
    stop = run_dir / "STOP"
    stop.write_text(f"{reason}: {source}\n")
    raise SystemExit(f"RRSI stopped: {reason}; STOP marker: {stop}; progress saved in {run_dir}")
