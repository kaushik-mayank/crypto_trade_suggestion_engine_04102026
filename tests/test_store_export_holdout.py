"""PHASE-05 — Parquet export (derived) and the holdout guard.

The holdout tests are the important ones: the guard is what makes "used exactly once" a property of
the system rather than a promise in a document.
"""
from decimal import Decimal

import pyarrow.parquet as pq
import pytest

from core.errors import ProtocolViolation
from normalize.canonical import Bar, Product, Timeframe
from store import open_store
from store.holdout import HoldoutGuard
from store.parquet_export import export_all, export_series

T0 = 1789725600000            # 2026-09-18T10:00:00Z
H = 3_600_000


@pytest.fixture
def store(tmp_path):
    s = open_store(tmp_path / "state.db")
    bars = [Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=Timeframe.H1,
                open_time_ms=T0 + i * H, open="100", high="110", low="90",
                close=f"10{i}.12345678", volume_base="1.5", is_final=True, fetched_at_ms=T0)
            for i in range(5)]
    s.upsert_bars(bars)
    yield s
    s.close()


# ---- parquet export ----------------------------------------------------------------------

def test_export_writes_one_file_per_month_with_exact_decimals(store, tmp_path):
    written = export_series(store, tmp_path / "parquet", "B-BTC_USDT", Product.FUTURES, Timeframe.H1)
    assert written == {"2026-09": 5}
    path = tmp_path / "parquet" / "futures" / "B-BTC_USDT" / "1h" / "2026-09.parquet"
    t = pq.read_table(path)
    assert t.num_rows == 5
    assert t.column("close").to_pylist()[0] == Decimal("100.12345678")     # exact, not a float
    assert t.column("time_ms").to_pylist() == sorted(t.column("time_ms").to_pylist())


def test_sqlite_and_parquet_agree_row_for_row(store, tmp_path):
    export_series(store, tmp_path / "p", "B-BTC_USDT", Product.FUTURES, Timeframe.H1)
    t = pq.read_table(tmp_path / "p" / "futures" / "B-BTC_USDT" / "1h" / "2026-09.parquet")
    from_sqlite = store.get_bars("B-BTC_USDT", Product.FUTURES, Timeframe.H1, 0, T0 + 10 * H)
    assert [b.open_time_ms for b in from_sqlite] == t.column("time_ms").to_pylist()
    assert [b.close for b in from_sqlite] == t.column("close").to_pylist()


def test_export_is_idempotent_and_replaces_atomically(store, tmp_path):
    out = tmp_path / "p"
    export_series(store, out, "B-BTC_USDT", Product.FUTURES, Timeframe.H1)
    export_series(store, out, "B-BTC_USDT", Product.FUTURES, Timeframe.H1)
    files = list((out / "futures" / "B-BTC_USDT" / "1h").iterdir())
    assert [f.name for f in files] == ["2026-09.parquet"]        # no .tmp left behind


def test_export_skips_non_final_bars_by_default(store, tmp_path):
    store.upsert_bars([Bar(asset="B-BTC_USDT", product=Product.FUTURES, timeframe=Timeframe.H1,
                           open_time_ms=T0 + 9 * H, open="1", high="1", low="1", close="1",
                           volume_base="0", is_final=False, fetched_at_ms=T0)])
    written = export_series(store, tmp_path / "p", "B-BTC_USDT", Product.FUTURES, Timeframe.H1)
    assert sum(written.values()) == 5                            # the non-final bar is excluded


def test_export_all_covers_every_series_present(store, tmp_path):
    store.upsert_bars([Bar(asset="B-ETH_USDT", product=Product.SPOT, timeframe=Timeframe.D1,
                           open_time_ms=1789689600000, open="1", high="1", low="1", close="1",
                           volume_base="1", is_final=True, fetched_at_ms=T0)])
    result = export_all(store, tmp_path / "p")
    assert set(result) == {"futures:B-BTC_USDT:1h", "spot:B-ETH_USDT:1d"}


# ---- holdout guard -----------------------------------------------------------------------

def test_holdout_is_locked_by_default(tmp_path):
    g = HoldoutGuard(tmp_path / "holdout")
    with pytest.raises(ProtocolViolation) as ei:
        g.path("2026-09.parquet")
    assert "requires an explicit, logged unlock" in ei.value.message
    assert "Stage 10" in ei.value.remediation
    assert g.status()["used"] is False


def test_unlocking_records_an_access_with_reason_and_protocol_hash(tmp_path):
    g = HoldoutGuard(tmp_path / "holdout", protocol_hash="abc123")
    g.unlock(reason="Stage 10 evaluation of the promoted set")
    assert g.path("x.parquet").name == "x.parquet"
    (access,) = g.accesses()
    assert access["event"] == "unlock" and access["protocol_hash"] == "abc123"
    assert "Stage 10" in access["reason"]
    assert g.used is True and g.burned is False


def test_unlocking_without_a_reason_is_refused(tmp_path):
    with pytest.raises(ProtocolViolation, match="requires a stated reason"):
        HoldoutGuard(tmp_path / "holdout").unlock(reason="   ")


def test_a_second_use_is_refused_and_says_what_it_would_cost(tmp_path):
    d = tmp_path / "holdout"
    HoldoutGuard(d).unlock(reason="first, legitimate use")
    with pytest.raises(ProtocolViolation) as ei:
        HoldoutGuard(d).unlock(reason="one more peek")
    assert "already been used once" in ei.value.message
    assert "BURNS the holdout" in ei.value.remediation
    assert ei.value.severity.value == "critical"


def test_a_deliberate_burn_is_permitted_and_recorded_as_such(tmp_path):
    d = tmp_path / "holdout"
    HoldoutGuard(d).unlock(reason="first use")
    g = HoldoutGuard(d).unlock(reason="deliberate second use, accepting the burn", allow_burn=True)
    assert g.burned is True
    assert (d / "BURNED").exists()
    assert [a["burn"] for a in g.accesses()] == [False, True]


def test_holdout_status_is_reportable_for_the_validation_report(tmp_path):
    g = HoldoutGuard(tmp_path / "holdout")
    assert g.status() == {"directory": str(tmp_path / "holdout"), "exists": False, "used": False,
                          "burned": False, "accesses": 0, "unlocked_in_this_process": False}
