"""Optional ranking after all native RRSI admissibility and cost gates."""
from __future__ import annotations

from rsi_eval.method_policy import policy_identity, select_candidate


def select_round(cands, incumbent, S_star, delta, cfg, incumbent_counts, guard_fn=None):
    from rrsi.selection import select_round as native_select

    winner, decisions = native_select(cands, incumbent, S_star, delta, cfg,
                                     incumbent_counts, guard_fn=guard_fn)
    domain = getattr(guard_fn, "__self__", None)
    policy = getattr(domain, "config", {}).get("method_policy", {})
    if not policy.get("selection"):
        return winner, decisions
    eligible = [{"id": c.variant, "score": c.ev.S, "tokens": c.ev.C,
                 "evaluation": c.ev.to_json(), "decision": d.to_json()}
                for c, d in zip(cands, decisions)
                if d.admissible and c.gate_failure is None and c.ev is not None
                and not c.ev.missing and c.ev.C is not None]
    native = winner.variant if winner is not None else None
    selected = select_candidate(policy, eligible, native,
                                context={"method": "rrsi", "incumbent": incumbent.to_json(),
                                         "S_star": S_star, "delta": delta})
    for decision in decisions:
        decision.__dict__["method_policy"] = policy_identity(policy)
        decision.__dict__["policy_selected"] = decision.variant == selected
        decision.__dict__["native_selected"] = decision.variant == native
    return next((c for c in cands if c.variant == selected), None), decisions
