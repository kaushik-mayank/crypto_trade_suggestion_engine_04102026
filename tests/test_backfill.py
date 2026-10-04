"""Backfill: chunking, dedup of overlapping pages, open-bar exclusion, gap counting,
verbatim raw pages, and exact Decimal values in parquet."""
import gzip
import json
from decimal import Decimal

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import backfill.__main__ as bf
from cdx_http.client import HttpResult, build_url

M = 60_000
T0 = 1789000000000 - 1789000000000 % M      # a minute boundary


class FakeFetcher:
    """Serves 1m spot bars for every minute in [T0, T0+10m) except T0+4m (a hole)."""

    def __init__(self, now):
        self.now = now
        self.requests = 0

    def get(self, url, params):
        self.requests += 1
        s, e = params["startTime"], params["endTime"]
        times = [t for t in range(T0, T0 + 10 * M, M) if s <= t <= e and t != T0 + 4 * M]
        rows = [{"open": 100 + i, "high": "101.5", "low": 99.25, "close": 100.00000001, "volume": 0.5, "time": t}
                for i, t in enumerate(times)]
        body = json.dumps(sorted(rows, key=lambda r: -r["time"])).replace('"101.5"', "101.5").encode()
        return HttpResult("GET", build_url(url, params), 200, {}, body, self.now - 50, self.now)


@pytest.fixture
def out(tmp_path, monkeypatch):
    monkeypatch.setattr(bf, "OUT", tmp_path)
    monkeypatch.setattr(bf, "DATA", tmp_path)
    return tmp_path


def test_backfill_series(out, monkeypatch):
    now = T0 + 9 * M + 30_000                   # the 9th-minute bar is still open
    monkeypatch.setattr(bf, "now_ms", lambda: now)
    f = FakeFetcher(now)
    st = bf.backfill_series(f, "spot", "B-BTC_USDT", "1m", M, T0, spot_page=4)
    # windows of 4 bar-opens sharing a boundary: [0,3],[3,6],[6,9],[9,9.5]
    assert f.requests == 4 and st["pages"] == 4
    assert st["bars"] == 8                       # minutes 0..8 minus the hole at 4
    assert st["duplicates_dropped"] == 2         # boundary bars 3 and 6 seen twice (9 is open)
    assert (st["gaps"], st["missing_bars"], st["largest_gap_bars"]) == (1, 1, 1)
    assert st["expected_bars_first_to_last"] == 9
    t = pq.read_table(out / "spot" / "B-BTC_USDT" / "1m.parquet")
    assert t.column("time_ms").to_pylist() == [T0 + i * M for i in (0, 1, 2, 3, 5, 6, 7, 8)]
    assert t.column("close").to_pylist()[0] == Decimal("100.00000001")
    assert t.column("open").to_pylist()[0] == Decimal("100")
    assert t.column("high").to_pylist()[0] == Decimal("101.5")
    raw = gzip.open(out / "raw" / "spot" / "B-BTC_USDT" / "1m.jsonl.gz", "rt").read().splitlines()
    assert len(raw) == 4 and json.loads(raw[0])["body"][0]["close"] == 100.00000001
    assert '"close": 100.00000001' in raw[0]          # body embedded verbatim, spacing and all
    assert not list(out.rglob("*.tmp"))


def test_excess_precision_raises_instead_of_rounding(out, monkeypatch):
    now = T0 + 3 * M
    monkeypatch.setattr(bf, "now_ms", lambda: now)

    class Precise(FakeFetcher):
        def get(self, url, params):
            r = super().get(url, params)
            body = r.body.replace(b"100.00000001", b"100.0000000000000000001")    # 19 decimals
            return HttpResult(r.method, r.url, 200, {}, body, r.sent_at_ms, r.received_at_ms)

    with pytest.raises(pa.ArrowInvalid):      # rescale would lose data -> refuse, never round
        bf.backfill_series(Precise(now), "spot", "B-BTC_USDT", "1m", M, T0, spot_page=500)
    assert not (out / "spot" / "B-BTC_USDT" / "1m.parquet").exists()     # no partial series published


def test_verify_series_structural_gap_and_short_steps(tmp_path):
    import pyarrow as pa
    D = 86_400_000
    # daily bars: day 0, then a 10-day hole, then days 11..20 with day 15 missing, plus one short step
    times = [0] + [d * D for d in range(11, 21) if d != 15]
    times.insert(3, 12 * D + D // 2)             # a bar half a day after day 12
    path = tmp_path / "x.parquet"
    pq.write_table(pa.table({"time_ms": pa.array(times, pa.int64())}), path)
    v = bf.verify_series(path, D)
    assert v["sorted_unique"] is True
    assert v["structural_gaps_ge_7d"] == [{"from": bf.iso(0), "to": bf.iso(11 * D), "missing_bars": 10}]
    assert v["continuous_from"] == bf.iso(11 * D)
    assert v["irregular_short_steps"] == 2       # 12 -> 12.5 and 12.5 -> 13
    assert v["missing_bars_after_continuous_start"] == 1          # day 15
    assert v["completeness_after_continuous_start"] == 0.9        # 1 - 1/10
    assert v["misaligned"] == 1
