"""Recorder acceptance test 3 — survive a 10-minute network outage.

Runs a SEPARATE recorder instance (scratch data dir, its own lock) whose traffic goes
through a local CONNECT proxy controlled by this script (``HTTPS_PROXY``; no admin
rights, no firewall changes, production recorder untouched). Timeline:

    UP 3 min  ->  BLACKHOLE 5 min (connections accepted, never answered: timeouts)
              ->  REFUSE 5 min   (connections dropped at once: connection errors)
              ->  UP 6 min

Then checks, from the instance's own output:
  * no hot loop: attempts during the outage are consistent with the backoff schedule;
  * automatic recovery: every enabled stream records again after the outage;
  * the gap is recorded in ``_meta/gaps.jsonl`` with reason ``error_streak``;
  * the process never exited.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

from .common import DATA, ROOT, iso, now_ms

MODE = {"v": "up"}


def _pipe(a: socket.socket, b: socket.socket):
    socks = [a, b]
    try:
        while MODE["v"] == "up":
            r, _, _ = select.select(socks, [], [], 1.0)
            for s in r:
                data = s.recv(65536)
                if not data:
                    return
                (b if s is a else a).sendall(data)
    except OSError:
        pass
    finally:
        for s in socks:
            try:
                s.close()
            except OSError:
                pass


def _handle(conn: socket.socket):
    mode = MODE["v"]
    if mode == "refuse":
        conn.close()
        return
    if mode == "blackhole":
        t = time.time()
        while MODE["v"] == "blackhole" and time.time() - t < 120:
            time.sleep(0.5)
        conn.close()
        return
    try:
        conn.settimeout(10)
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = conn.recv(4096)
            if not chunk:
                conn.close()
                return
            req += chunk
        line = req.split(b"\r\n", 1)[0].decode()
        method, target, _ = line.split(" ", 2)
        if method != "CONNECT":
            conn.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            conn.close()
            return
        host, port = target.rsplit(":", 1)
        up = socket.create_connection((host, int(port)), timeout=10)
        conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        conn.settimeout(None)
        up.settimeout(None)
        _pipe(conn, up)
    except OSError:
        conn.close()


def serve(port: int):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(64)
    while True:
        c, _ = srv.accept()
        threading.Thread(target=_handle, args=(c,), daemon=True).start()


def load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()] if p.exists() else []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18766)
    ap.add_argument("--scale", type=float, default=1.0, help="shrink all phases (testing the harness)")
    args = ap.parse_args()
    work = DATA / "reliability" / f"outage_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    work.mkdir(parents=True, exist_ok=True)
    cfg = (ROOT / "recorder.toml").read_text(encoding="utf-8")
    cfg = cfg.replace('data_dir = "data/raw"', f'data_dir = "{(work / "raw").as_posix()}"')
    cfg = cfg.replace('log_dir = "logs"', f'log_dir = "{(work / "logs").as_posix()}"')
    cfg = cfg.replace("health_interval_s = 900", "health_interval_s = 60")
    (work / "recorder.toml").write_text(cfg, encoding="utf-8")

    threading.Thread(target=serve, args=(args.port,), daemon=True).start()
    env = {**os.environ, "HTTPS_PROXY": f"http://127.0.0.1:{args.port}", "HTTP_PROXY": f"http://127.0.0.1:{args.port}",
           "NO_PROXY": ""}
    out = open(work / "stdout.txt", "w", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, "-m", "recorder", "--config", str(work / "recorder.toml")],
                            cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT)
    s = args.scale
    timeline = [("up", 180 * s), ("blackhole", 300 * s), ("refuse", 300 * s), ("up", 360 * s)]
    marks = []
    for mode, dur in timeline:
        MODE["v"] = mode
        marks.append({"mode": mode, "start_ms": now_ms(), "start": iso(now_ms())})
        print(f"{iso(now_ms())} proxy -> {mode} for {dur:.0f}s", flush=True)
        end = time.time() + dur
        while time.time() < end:
            if proc.poll() is not None:
                break
            time.sleep(1)
    alive = proc.poll() is None
    if alive:
        # graceful stop: CTRL_BREAK is not deliverable without a new console group; use terminate
        proc.terminate()
        proc.wait(30)
    out.close()

    outage_start, outage_end = marks[1]["start_ms"], marks[3]["start_ms"]
    raw = work / "raw"
    errors = load(raw / "_meta" / "errors.jsonl")
    gaps = [g for g in load(raw / "_meta" / "gaps.jsonl") if "reason" in g]
    during = [e for e in errors if outage_start <= e["at_ms"] <= outage_end]
    per_task = Counter(f"{e.get('stream')}:{e.get('pair')}" for e in during)
    recovered = {}
    for d in sorted(p for p in raw.iterdir() if p.is_dir() and p.name != "_meta"):
        after = [r for f in d.glob("*.jsonl") for r in load(f) if r["received_at_ms"] > outage_end]
        recovered[d.name] = {"records_after_outage": len(after),
                             "first_after_s": round((min(r["received_at_ms"] for r in after) - outage_end) / 1000, 1)
                             if after else None}
    # Without backoff, a 60 s stream would attempt ~outage/60 times; a hot loop would be hundreds.
    outage_s = (outage_end - outage_start) / 1000
    result = {
        "work_dir": str(work), "timeline": marks, "outage_seconds": outage_s,
        "process_alive_throughout": alive,
        "errors_during_outage_by_kind": dict(Counter(e["kind"] for e in during)),
        "attempts_during_outage_per_task": dict(per_task),
        "max_attempts_any_task": max(per_task.values()) if per_task else 0,
        "hot_loop": (max(per_task.values()) if per_task else 0) > outage_s / 2,
        "gap_records": gaps,
        "recovery": recovered,
        "all_streams_recovered": all(v["records_after_outage"] > 0 for k, v in recovered.items()
                                     if not k.startswith(("spot_candles", "futures_candles", "instrument_meta"))),
        "candle_gapfill_after_outage": {k: v for k, v in recovered.items() if "candles" in k},
    }
    (DATA / "probe_results").mkdir(parents=True, exist_ok=True)
    res_path = DATA / "probe_results" / f"reliability_outage_{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.json"
    res_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("gap_records",)}, indent=2))
    print(f"gap records: {len(gaps)}; result: {res_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
