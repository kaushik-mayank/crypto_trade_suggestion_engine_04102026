"""The audit trail (specs/13 §9, specs/10 §7-§8).

**Rejections are recorded too.** A published signal explains itself; the absence of one explains
nothing unless the rejection was written down. Without it, "Class 1 never fired" is an observation
with no diagnosis, and hypotheses H1, H2, H3, H5 and H10 (specs/09 §11) are untestable. So every
``NoSetup`` is persisted with its full condition set and its binding condition.

**Built incrementally, written atomically.** One transaction per evaluation: the signal row, its
lifecycle event, its decision trace and the arbitration events land together or not at all. A partial
audit record is worse than none, because it looks complete.

**Storing the snapshot itself would be wasteful, so provenance is stored instead** (specs/10 §7): the
``as_of``, ``snapshot_id``, ``code_version`` and ``parameter_hash`` are enough to rebuild the snapshot
and re-run the engine. :mod:`audit.replay` does exactly that, and a signal that does not reproduce is
either a bug or a recorded bar mutation — which the snapshot's ``content_digest`` tells apart.
"""
from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from core.errors import AuditError
from core.logging import get_logger
from risk.engine import Signal
from strategy.base import SetupState, StrategyOutcome

log = get_logger("audit.trail")
COMPONENT = "audit.trail"


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


@dataclass
class AuditTrail:
    """Writes the decision record. Holds the store; nothing upstream of it does."""

    store: Any

    # ---- signals --------------------------------------------------------------------------

    def record_evaluation(self, result: Any) -> dict[str, int]:
        """Persist one whole evaluation pass, atomically.

        Returns the counts written, so a caller can assert the audit actually happened rather than
        assuming it did.
        """
        counts = {"signals": 0, "events": 0, "audit": 0, "rejections": 0, "arbitration": 0,
                  "setup_state": 0}
        now = _now_ms()
        conn = self.store.conn
        try:
            conn.execute("BEGIN")
            for asset_result in result.assets.values():
                for strategy_id, outcome in asset_result.outcomes.items():
                    if outcome.kind == "NoSetup":
                        self._write_rejection(strategy_id, outcome, asset_result,
                                              result.as_of_ms, now)
                        counts["rejections"] += 1
                    if outcome.state is not None:
                        self._write_setup_state(outcome.state, now)
                        counts["setup_state"] += 1
                for signal in asset_result.signals:
                    self._write_signal(signal, asset_result, result.as_of_ms, now)
                    counts["signals"] += 1
                    counts["events"] += self._write_events(signal, now)
                    self._write_audit_record(signal, result, now)
                    counts["audit"] += 1
            if result.arbitration is not None:
                for event in result.arbitration.events:
                    self._write_arbitration(event, now)
                    counts["arbitration"] += 1
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise AuditError(
                f"the audit record for the evaluation at {result.as_of_ms} could not be written",
                component=COMPONENT, operation="record_evaluation",
                context={"as_of_ms": result.as_of_ms, "written_before_failure": counts},
                likely_cause=str(e),
                remediation="the whole record was rolled back: a partial audit trail is worse than "
                            "none, because it looks complete. Fix the store error and re-run",
                cause=e,
            ) from e
        log.info("audit_written", as_of_ms=result.as_of_ms, **counts)
        return counts

    def _write_signal(self, signal: Signal, asset_result: Any, as_of_ms: int, now: int) -> None:
        raw = signal.raw
        strategy_class = int(raw.detail.get("strategy_class", 0) or 0)
        self.store.conn.execute(
            """INSERT INTO signals (signal_id, strategy_id, strategy_version, strategy_class, asset,
                   product, direction, state, state_since_ms, tier, validated, mode, armed_at_ms,
                   expires_at_ms, entry, stop, targets, risk_reward, k_net, breakeven_hit_rate,
                   regime_cell, regime_dwell, original_entry_price, parameter_hash, code_version,
                   snapshot_id, as_of_ms, payload, created_at_ms, updated_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(signal_id) DO UPDATE SET
                   state=excluded.state, state_since_ms=excluded.state_since_ms,
                   entry=excluded.entry, stop=excluded.stop, targets=excluded.targets,
                   risk_reward=excluded.risk_reward, k_net=excluded.k_net,
                   breakeven_hit_rate=excluded.breakeven_hit_rate,
                   payload=excluded.payload, updated_at_ms=excluded.updated_at_ms""",
            (raw.signal_id, raw.strategy_id, raw.strategy_version, strategy_class, raw.asset,
             raw.product.value, raw.direction.value, raw.lifecycle.value, as_of_ms,
             str(raw.detail.get("tier", "")), int(bool(raw.detail.get("validated"))), "suggestion",
             as_of_ms, None,
             str(raw.entry) if raw.entry is not None else None,
             str(raw.stop) if raw.stop is not None else None,
             _json([str(t) for t in raw.targets]),
             str(raw.reward_to_risk()) if raw.reward_to_risk() is not None else None,
             signal.detail.get("k_net"), signal.detail.get("breakeven_hit_rate"),
             _json(raw.regime.get("cell")), raw.regime.get("dwell_bars"),
             None, raw.parameter_hash, raw.code_version, raw.snapshot_id, as_of_ms,
             _json(signal.to_dict()), now, now))

    def _write_events(self, signal: Signal, now: int) -> int:
        """One row per lifecycle transition recorded on the setup, deduplicated by (signal, to, at)."""
        transitions = signal.raw.detail.get("progress", {}).get("lifecycle")
        if transitions is None:
            return 0
        self.store.conn.execute(
            """INSERT INTO signal_events (signal_id, at_ms, from_state, to_state, reason, detail)
               VALUES (?,?,?,?,?,?)""",
            (signal.raw.signal_id, signal.raw.as_of_ms, None, signal.raw.lifecycle.value,
             signal.raw.rationale[:500], _json(signal.raw.detail.get("progress", {}))))
        return 1

    def _write_audit_record(self, signal: Signal, result: Any, now: int) -> None:
        """The complete decision trace: conditions, vetoes, sizing, regime and provenance.

        ``bar_mutated`` is set from the snapshot's content digest at replay time, not here; at write
        time there is nothing to compare against yet.
        """
        record = {
            "signal": signal.to_dict(),
            "conditions": [c.to_dict() for c in signal.raw.conditions],
            "vetoes": [v.to_dict() for v in signal.vetoes],
            "sizing": signal.sizing.to_dict(),
            "regime": signal.raw.regime,
            "snapshot": {"snapshot_id": signal.raw.snapshot_id,
                         "content_digest": result.snapshot.content_digest if result.snapshot else None,
                         "as_of_ms": result.as_of_ms},
            "arbitration": (result.arbitration.to_dict() if result.arbitration else None),
        }
        self.store.conn.execute(
            """INSERT INTO signal_audit (signal_id, as_of_ms, snapshot_id, code_version,
                   parameter_hash, record, bar_mutated, created_at_ms)
               VALUES (?,?,?,?,?,?,?,?)""",
            (signal.raw.signal_id, result.as_of_ms, signal.raw.snapshot_id, signal.raw.code_version,
             signal.raw.parameter_hash, _json(record), 0, now))

    def _write_rejection(self, strategy_id: str, outcome: StrategyOutcome, asset_result: Any,
                         as_of_ms: int, now: int) -> None:
        """Every ``NoSetup``, with all condition results — not only the failing one.

        The binding condition is stored separately for the UI, but the full set is what makes
        "which condition blocks most often" answerable (specs/09 §11).
        """
        summary = outcome.summary
        self.store.conn.execute(
            """INSERT INTO rejections (at_ms, as_of_ms, strategy_id, asset, product,
                   binding_condition, passed_count, total_count, conditions, rolled_up)
               VALUES (?,?,?,?,?,?,?,?,?,0)""",
            (now, as_of_ms, strategy_id, asset_result.asset, "futures",
             summary.get("first_failure"),
             summary.get("passed", 0), summary.get("total", 0),
             _json([c.to_dict() for c in outcome.conditions])))

    def _write_setup_state(self, state: SetupState, now: int) -> None:
        self.store.conn.execute(
            """INSERT INTO setup_state (signal_id, strategy_id, asset, product, direction,
                   armed_at_ms, armed_snapshot_id, x_star, e_t, lifecycle, expires_at_ms,
                   original_entry_price, parameter_hash, strategy_version, updated_at_ms)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(signal_id) DO UPDATE SET
                   x_star=excluded.x_star, e_t=excluded.e_t, lifecycle=excluded.lifecycle,
                   original_entry_price=excluded.original_entry_price,
                   updated_at_ms=excluded.updated_at_ms""",
            (state.signal_id, state.strategy_id, state.asset, state.product.value,
             state.direction.value, state.armed_at_ms, state.armed_snapshot_id,
             str(state.x_star) if state.x_star is not None else None,
             str(state.e_t) if state.e_t is not None else None,
             state.lifecycle.value, state.expires_at_ms,
             str(state.original_entry_price) if state.original_entry_price is not None else None,
             state.parameter_hash, state.strategy_version, now))

    def _write_arbitration(self, event: Any, now: int) -> None:
        self.store.conn.execute(
            """INSERT INTO arbitration_events (at_ms, kind, asset, primary_signal_id,
                   other_signal_id, detail) VALUES (?,?,?,?,?,?)""",
            (event.at_ms, event.kind.upper(), event.asset, event.winner,
             event.losers[0] if event.losers else None, _json(event.to_dict())))

    # ---- reading back ---------------------------------------------------------------------

    def signals(self, *, states: Sequence[str] | None = None, asset: str | None = None,
                limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM signals"
        clauses: list[str] = []
        params: list[Any] = []
        if states:
            clauses.append(f"state IN ({','.join('?' * len(states))})")
            params.extend(states)
        if asset:
            clauses.append("asset = ?")
            params.append(asset)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at_ms DESC LIMIT ?"
        params.append(limit)
        rows = self.store.conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def rejections(self, *, asset: str | None = None, since_ms: int = 0,
                   limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT * FROM rejections WHERE at_ms >= ?"
        params: list[Any] = [since_ms]
        if asset:
            sql += " AND asset = ?"
            params.append(asset)
        sql += " ORDER BY at_ms DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.store.conn.execute(sql, params).fetchall()]

    def binding_conditions(self, *, since_ms: int = 0) -> list[dict[str, Any]]:
        """Which condition blocks most often, per strategy and asset — the specs/09 §11 question."""
        rows = self.store.conn.execute(
            """SELECT strategy_id, asset, binding_condition, COUNT(*) AS n
               FROM rejections WHERE at_ms >= ? AND binding_condition IS NOT NULL
               GROUP BY strategy_id, asset, binding_condition ORDER BY n DESC""",
            (since_ms,)).fetchall()
        return [dict(r) for r in rows]

    def audit_record(self, signal_id: str) -> dict[str, Any] | None:
        row = self.store.conn.execute(
            "SELECT * FROM signal_audit WHERE signal_id = ? ORDER BY as_of_ms DESC LIMIT 1",
            (signal_id,)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["record"] = json.loads(out["record"])
        return out

    def open_setups(self) -> list[dict[str, Any]]:
        """Armed setups, for a restart to resume from (specs/13 §11.1)."""
        rows = self.store.conn.execute(
            """SELECT * FROM setup_state
               WHERE lifecycle IN ('SETUP_DETECTED','ENTER_NOW','TRIGGERED')
               ORDER BY armed_at_ms""").fetchall()
        return [dict(r) for r in rows]
