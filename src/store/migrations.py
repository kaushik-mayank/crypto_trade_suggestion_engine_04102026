"""Schema migrations.

Forward-only, numbered, applied in one transaction each, recorded in ``schema_version``. A gap or an
unknown applied version is a :class:`MigrationError` — never a silent "best effort" schema, because
a half-migrated database that accepts writes corrupts the source of truth.

**Decimal storage:** SQLite has no decimal type, and REAL would silently round exchange prices. Every
price and quantity is stored as **TEXT** and read back through ``Decimal``. That is the only
representation that survives a round trip exactly (invariant 5).
"""
from __future__ import annotations

MIGRATIONS: list[tuple[int, str, str]] = [
    (
        1,
        "core market data, dq events, runs",
        """
        CREATE TABLE bars (
            asset          TEXT    NOT NULL,
            product        TEXT    NOT NULL,
            timeframe      TEXT    NOT NULL,
            open_time_ms   INTEGER NOT NULL,
            open           TEXT    NOT NULL,
            high           TEXT    NOT NULL,
            low            TEXT    NOT NULL,
            close          TEXT    NOT NULL,
            volume_base    TEXT    NOT NULL,
            is_final       INTEGER NOT NULL DEFAULT 0,
            synthesised    INTEGER NOT NULL DEFAULT 0,
            source         TEXT    NOT NULL DEFAULT '',
            content_hash   TEXT    NOT NULL,
            first_fetched_ms INTEGER NOT NULL,
            last_fetched_ms  INTEGER NOT NULL,
            fetch_count    INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (asset, product, timeframe, open_time_ms)
        ) WITHOUT ROWID;

        CREATE INDEX idx_bars_lookup ON bars (asset, product, timeframe, is_final, open_time_ms);

        CREATE TABLE known_absent_bars (
            asset          TEXT    NOT NULL,
            product        TEXT    NOT NULL,
            timeframe      TEXT    NOT NULL,
            open_time_ms   INTEGER NOT NULL,
            confirmations  INTEGER NOT NULL DEFAULT 1,
            first_confirmed_ms INTEGER NOT NULL,
            last_confirmed_ms  INTEGER NOT NULL,
            PRIMARY KEY (asset, product, timeframe, open_time_ms)
        ) WITHOUT ROWID;

        CREATE TABLE futures_rt (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            funding_rate TEXT NOT NULL, predicted_funding_rate TEXT NOT NULL,
            mark_price TEXT NOT NULL, last_price TEXT NOT NULL,
            high_24h TEXT, low_24h TEXT, volume_quote_24h TEXT, price_change_pct TEXT,
            skw INTEGER, tpe_tick_send_ms INTEGER, tpe_mark_send_ms INTEGER,
            received_at_ms INTEGER NOT NULL,
            source_ts_ms INTEGER NOT NULL,           -- the row's own time: freshness comes from here
            PRIMARY KEY (asset, source_ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE spot_ticker (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            last_price TEXT NOT NULL, bid TEXT, ask TEXT,
            high_24h TEXT, low_24h TEXT, volume_quote_24h TEXT, change_pct_24h TEXT,
            received_at_ms INTEGER NOT NULL,
            PRIMARY KEY (asset, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE positioning (
            asset TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            long_count_pct TEXT NOT NULL, short_count_pct TEXT NOT NULL,
            long_value_pct TEXT NOT NULL, short_value_pct TEXT NOT NULL,
            PRIMARY KEY (asset, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE book_snapshots (
            asset TEXT NOT NULL, product TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            bids TEXT NOT NULL, asks TEXT NOT NULL,      -- JSON [[price, qty], ...] as strings
            version INTEGER, received_at_ms INTEGER NOT NULL,
            best_bid TEXT NOT NULL, best_ask TEXT NOT NULL, levels INTEGER NOT NULL,
            PRIMARY KEY (asset, product, ts_ms)
        ) WITHOUT ROWID;

        CREATE TABLE trades (
            asset TEXT NOT NULL, product TEXT NOT NULL, ts_ms INTEGER NOT NULL,
            price TEXT NOT NULL, qty TEXT NOT NULL, is_buyer_maker INTEGER NOT NULL,
            PRIMARY KEY (asset, product, ts_ms, price, qty, is_buyer_maker)
        ) WITHOUT ROWID;

        CREATE TABLE instrument_meta (
            asset TEXT NOT NULL, product TEXT NOT NULL, fetched_at_ms INTEGER NOT NULL,
            tick TEXT NOT NULL, qty_increment TEXT NOT NULL, min_notional TEXT NOT NULL,
            maker_fee_fraction TEXT NOT NULL, taker_fee_fraction TEXT NOT NULL,
            multiplier_up TEXT, multiplier_down TEXT, funding_frequency_h INTEGER,
            max_leverage TEXT, margin_tiers TEXT NOT NULL, status TEXT NOT NULL,
            PRIMARY KEY (asset, product, fetched_at_ms)
        ) WITHOUT ROWID;

        CREATE TABLE dq_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL, kind TEXT NOT NULL,
            component TEXT NOT NULL, operation TEXT NOT NULL,
            problem_class TEXT, severity TEXT NOT NULL DEFAULT 'warning',
            asset TEXT, detail TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_dq_events_time ON dq_events (at_ms, kind);

        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            process TEXT NOT NULL, started_ms INTEGER NOT NULL, stopped_ms INTEGER,
            code_version TEXT NOT NULL, parameter_hash TEXT NOT NULL,
            settings_hash TEXT NOT NULL DEFAULT '', stop_reason TEXT, notes TEXT NOT NULL DEFAULT ''
        );
        """,
    ),
    (
        2,
        "signals, lifecycle, audit, rejections, arbitration (specs/10 §7)",
        """
        CREATE TABLE signals (
            signal_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL, strategy_version TEXT NOT NULL, strategy_class INTEGER NOT NULL,
            asset TEXT NOT NULL, product TEXT NOT NULL, direction TEXT NOT NULL,
            state TEXT NOT NULL, state_since_ms INTEGER NOT NULL,
            tier TEXT NOT NULL, validated INTEGER NOT NULL DEFAULT 0,
            mode TEXT NOT NULL DEFAULT 'suggestion',
            armed_at_ms INTEGER NOT NULL, expires_at_ms INTEGER,
            entry TEXT, stop TEXT, targets TEXT, trail_level TEXT,
            risk_reward TEXT, remaining_rr TEXT, k_net TEXT, breakeven_hit_rate TEXT,
            strength TEXT, confidence TEXT, confidence_unavailable_reason TEXT,
            regime_cell TEXT, regime_dwell INTEGER,
            trigger_price TEXT, trigger_time_ms INTEGER,
            original_entry_price TEXT, original_entry_ms INTEGER,
            handoff_from TEXT, handoff_to TEXT, suppressed_by TEXT,
            parameter_hash TEXT NOT NULL, code_version TEXT NOT NULL,
            snapshot_id TEXT NOT NULL, as_of_ms INTEGER NOT NULL,
            payload TEXT NOT NULL,                       -- the full published Signal object as JSON
            created_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL
        );
        CREATE INDEX idx_signals_state ON signals (state, asset, strategy_id);
        CREATE INDEX idx_signals_created ON signals (created_at_ms, signal_id);

        CREATE TABLE signal_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL, at_ms INTEGER NOT NULL,
            from_state TEXT, to_state TEXT NOT NULL, reason TEXT, detail TEXT NOT NULL DEFAULT '{}',
            FOREIGN KEY (signal_id) REFERENCES signals (signal_id)
        );
        CREATE INDEX idx_signal_events_time ON signal_events (at_ms, id);

        CREATE TABLE signal_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL, as_of_ms INTEGER NOT NULL, snapshot_id TEXT NOT NULL,
            code_version TEXT NOT NULL, parameter_hash TEXT NOT NULL,
            record TEXT NOT NULL,                        -- the complete decision trace, JSON
            bar_mutated INTEGER NOT NULL DEFAULT 0,
            created_at_ms INTEGER NOT NULL
        );
        CREATE INDEX idx_signal_audit_signal ON signal_audit (signal_id, as_of_ms);

        CREATE TABLE rejections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL, as_of_ms INTEGER NOT NULL,
            strategy_id TEXT NOT NULL, asset TEXT NOT NULL, product TEXT NOT NULL,
            binding_condition TEXT,                      -- the first failing condition, for the UI
            passed_count INTEGER NOT NULL, total_count INTEGER NOT NULL,
            conditions TEXT NOT NULL,                    -- ALL results, JSON (rolled up after 30 days)
            rolled_up INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_rejections_lookup ON rejections (asset, strategy_id, at_ms);

        CREATE TABLE rejection_rollup (
            day TEXT NOT NULL, strategy_id TEXT NOT NULL, asset TEXT NOT NULL,
            condition_name TEXT NOT NULL, failures INTEGER NOT NULL, evaluations INTEGER NOT NULL,
            PRIMARY KEY (day, strategy_id, asset, condition_name)
        ) WITHOUT ROWID;

        CREATE TABLE arbitration_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at_ms INTEGER NOT NULL, kind TEXT NOT NULL,   -- CONFLICT | MERGE | DEDUP | HANDOFF
            asset TEXT, primary_signal_id TEXT, other_signal_id TEXT,
            detail TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_arbitration_time ON arbitration_events (at_ms, kind);

        CREATE TABLE setup_state (
            signal_id TEXT PRIMARY KEY,
            strategy_id TEXT NOT NULL, asset TEXT NOT NULL, product TEXT NOT NULL,
            direction TEXT NOT NULL, armed_at_ms INTEGER NOT NULL, armed_snapshot_id TEXT NOT NULL,
            x_star TEXT, e_t TEXT, lifecycle TEXT NOT NULL, expires_at_ms INTEGER,
            original_entry_price TEXT, parameter_hash TEXT NOT NULL, strategy_version TEXT NOT NULL,
            updated_at_ms INTEGER NOT NULL
        );
        """,
    ),
]


def latest_version() -> int:
    return max(v for v, _, _ in MIGRATIONS)
