"""Explicit public paper-policy migration; never resets balances or accesses credentials."""

import json
import os
import re
from dataclasses import asdict

from ..errors import ConfigurationError
from .runner import atomic_json
from .tactical_engine import experiment


def migrate_flat_state(data, settings, previous_id, now):
    if not re.fullmatch(r"[0-9a-f]{20}", previous_id or ""):
        raise ConfigurationError("Use a recorded public experiment identifier.")
    new_id = experiment(settings)
    if new_id == previous_id:
        raise ConfigurationError("The requested policy already identifies this experiment.")
    acquired = []
    try:
        # Both normal workers use these locks. Never copy a ledger they can still change.
        for name in ("scan.lock", "paper.lock"):
            path = data / name
            with path.open("x", encoding="ascii") as handle:
                handle.write(str(os.getpid()))
            acquired.append(path)
        source = data / "experiments" / previous_id / "state.json"
        target = data / "experiments" / new_id / "state.json"
        if target.exists():
            raise ConfigurationError("Destination ledger already exists; never overwrite it.")
        state = json.loads(source.read_text(encoding="utf-8"))
        if state.get("experiment") != previous_id:
            raise ConfigurationError("Source ledger identity mismatch.")
        if set(state["profiles"]) != {str(n) for n in settings.leverages}:
            raise ConfigurationError("Migration cannot add or drop an existing paper scenario.")
        if any(p["book"]["qty"] for p in state["profiles"].values()):
            raise ConfigurationError("Policy migration requires all paper positions to be flat.")
        # No book, fee, loss, halt, cooldown, consumed decision or historical curve is reset.
        state["experiment"] = new_id
        state.setdefault("policy_history", []).append(
            dict(
                time=now,
                previous_experiment=previous_id,
                experiment=new_id,
                reason="user-requested-per-trade-equity-budget",
                settings=asdict(settings),
                opening_equity={k: p["book"]["cash"] for k, p in state["profiles"].items()},
            )
        )
        atomic_json(target, state)
        return dict(
            previous_experiment=previous_id,
            experiment=new_id,
            balances_preserved=state["policy_history"][-1]["opening_equity"],
        )
    finally:
        for path in reversed(acquired):
            path.unlink()
