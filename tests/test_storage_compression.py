"""Completed-day gzip compression, and the single-instance lock."""
import gzip
import hashlib
import json

from recorder.runner import Recorder
from recorder.storage import JsonlStore, SingleInstanceLock, compress_day_file, iter_records
from recorder.tasks import FuturesRtTask

# Includes a torn line, CRLF inside a string-free region, and non-ASCII: all must survive exactly.
RAW = b'{"a":1,"fr":0.00007903}\n{"torn":\n{"b":"\xe2\x80\x94"}\r\n{"c":3}\n'

DAY1 = 1789689600000          # 2026-09-18T00:00:00Z
DAY2 = DAY1 + 86_400_000      # 2026-09-19T00:00:00Z


def test_compress_is_byte_exact(tmp_path):
    f = tmp_path / "2026-09-18.jsonl"
    f.write_bytes(RAW)
    info = compress_day_file(f)
    gz = tmp_path / "2026-09-18.jsonl.gz"
    assert not f.exists() and gz.exists()
    assert gzip.decompress(gz.read_bytes()) == RAW
    assert info["sha256_raw"] == hashlib.sha256(RAW).hexdigest()
    assert info["raw_bytes"] == len(RAW) and info["action"] == "compressed"


def test_redundant_jsonl_after_crash_is_removed(tmp_path):
    f = tmp_path / "2026-09-18.jsonl"
    (tmp_path / "2026-09-18.jsonl.gz").write_bytes(gzip.compress(RAW))
    f.write_bytes(RAW)                                   # crash between rename and unlink
    assert compress_day_file(f)["action"] == "removed_redundant_jsonl"
    assert not f.exists()
    assert gzip.decompress((tmp_path / "2026-09-18.jsonl.gz").read_bytes()) == RAW


def test_late_records_are_merged_after_existing(tmp_path):
    (tmp_path / "2026-09-18.jsonl.gz").write_bytes(gzip.compress(b'{"a":1}\n{"torn'))
    f = tmp_path / "2026-09-18.jsonl"
    f.write_bytes(b'{"late":1}\n')
    assert compress_day_file(f)["action"] == "merged"
    # separator newline keeps the torn fragment isolated, exactly as the live writer does
    assert gzip.decompress((tmp_path / "2026-09-18.jsonl.gz").read_bytes()) == b'{"a":1}\n{"torn\n{"late":1}\n'


def test_compress_completed_skips_today_and_meta_and_cleans_tmp(tmp_path):
    s = tmp_path / "futures_rt"
    s.mkdir()
    (s / "2026-09-17.jsonl").write_bytes(b'{"x":1}\n')
    (s / "2026-09-18.jsonl").write_bytes(b'{"x":2}\n')
    (s / "2026-09-17.jsonl.gz.tmp").write_bytes(b"partial")
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "gaps.jsonl").write_bytes(b"{}\n")
    store = JsonlStore(tmp_path)
    done = store.compress_completed("2026-09-18")
    store.close()
    assert [d["action"] for d in done] == ["compressed"]
    assert sorted(p.name for p in s.iterdir()) == ["2026-09-17.jsonl.gz", "2026-09-18.jsonl"]
    assert (tmp_path / "_meta" / "gaps.jsonl").exists()
    comp = [json.loads(l) for l in (tmp_path / "_meta" / "compression.jsonl").read_text().splitlines()]
    assert comp[0]["stream"] == "futures_rt" and comp[0]["raw_bytes"] == 8


def test_open_handle_for_past_day_is_closed_then_compressed(tmp_path):
    store = JsonlStore(tmp_path)
    store.append("s", DAY1 + 5, '{"d":1}')               # handle open on 2026-09-18
    store.compress_completed("2026-09-19")
    store.append("s", DAY2 + 5, '{"d":2}')
    store.close()
    assert gzip.decompress((tmp_path / "s" / "2026-09-18.jsonl.gz").read_bytes()) == b'{"d":1}\n'
    assert (tmp_path / "s" / "2026-09-19.jsonl").read_bytes() == b'{"d":2}\n'


def test_day_files_order_and_gz_reading(tmp_path):
    s = tmp_path / "s"
    s.mkdir()
    (s / "2026-09-17.jsonl.gz").write_bytes(gzip.compress(b'{"n":17}\n'))
    (s / "2026-09-18.jsonl.gz").write_bytes(gzip.compress(b'{"n":180}\n'))
    (s / "2026-09-18.jsonl").write_bytes(b'{"n":181}\n')
    (s / "notes.txt").write_text("ignored")
    files = JsonlStore(tmp_path).day_files("s")
    assert [p.name for p in files] == ["2026-09-18.jsonl", "2026-09-18.jsonl.gz", "2026-09-17.jsonl.gz"]
    assert [r["n"] for p in files for r in iter_records(p)] == [181, 180, 17]


class _Http:
    def request(self, *a, **k):
        raise AssertionError("no network in this test")


def test_recorder_startup_compresses_yesterday_and_resumes_from_gz(tmp_path):
    rt = tmp_path / "futures_rt"
    rt.mkdir()
    line = {"stream": "futures_rt", "pair": "B-BTC_USDT", "received_at_ms": DAY2 - 1000, "http_status": 200}
    (rt / "2026-09-18.jsonl").write_text(json.dumps(line) + "\n")
    task = FuturesRtTask("futures_rt", None, "public.coindcx.com", 60, pairs=["B-BTC_USDT"])
    rec = Recorder([task], _Http(), JsonlStore(tmp_path), clock_ms=lambda: DAY2 + 30_000, sleep=lambda s: None)
    rec.startup()
    rec.store.close()
    assert not (rt / "2026-09-18.jsonl").exists() and (rt / "2026-09-18.jsonl.gz").exists()
    assert rec.state["futures_rt"].last_success_ms == DAY2 - 1000     # read back from the .gz


def test_day_rollover_in_tick_compresses(tmp_path):
    t = {"now": DAY1 + 3_600_000}
    task = FuturesRtTask("futures_rt", None, "public.coindcx.com", 60, pairs=["B-BTC_USDT"])
    rec = Recorder([task], _Http(), JsonlStore(tmp_path), clock_ms=lambda: t["now"], sleep=lambda s: None)
    rec.startup()
    rec.state["futures_rt"].stopped = True               # no polling in this test
    rec.store.append("futures_rt", t["now"], '{"x":1}')
    rec.tick()
    assert (tmp_path / "futures_rt" / "2026-09-18.jsonl").exists()
    t["now"] = DAY2 + 1
    rec.tick()
    rec.store.close()
    assert gzip.decompress((tmp_path / "futures_rt" / "2026-09-18.jsonl.gz").read_bytes()) == b'{"x":1}\n'


def test_single_instance_lock(tmp_path):
    a = SingleInstanceLock(tmp_path / "_meta" / "recorder.lock")
    b = SingleInstanceLock(tmp_path / "_meta" / "recorder.lock")
    assert a.acquire()
    assert not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_stop_file_triggers_clean_shutdown(tmp_path):
    task = FuturesRtTask("futures_rt", None, "public.coindcx.com", 60, pairs=["B-BTC_USDT"])
    rec = Recorder([task], _Http(), JsonlStore(tmp_path), clock_ms=lambda: DAY2, sleep=lambda s: None)
    # the stop request arrives while running (a pre-existing one is cleared as stale)
    rec.tasks[0].run = lambda ctx: (tmp_path / "_meta" / "STOP").write_text("")
    calls = {"n": 0}

    def guard(_s):
        calls["n"] += 1
        assert calls["n"] < 5, "recorder did not stop on the STOP file"
    rec._sleep = guard
    rec.run_forever()                                    # returns instead of looping forever
    events = [json.loads(l)["event"] for l in (tmp_path / "_meta" / "gaps.jsonl").read_text().splitlines()]
    assert events == ["startup", "shutdown"]
    assert not (tmp_path / "_meta" / "STOP").exists()


def test_stale_stop_file_is_cleared_at_startup(tmp_path):
    task = FuturesRtTask("futures_rt", None, "public.coindcx.com", 60, pairs=["B-BTC_USDT"])
    (tmp_path / "_meta").mkdir()
    (tmp_path / "_meta" / "STOP").write_text("")
    rec = Recorder([task], _Http(), JsonlStore(tmp_path), clock_ms=lambda: DAY2, sleep=lambda s: None)
    rec.startup()
    rec.store.close()
    assert not (tmp_path / "_meta" / "STOP").exists()
    assert rec._stop_requested() is False
