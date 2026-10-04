"""P9 + P10 — one synchronous sampler, two questions.

Every ``INTERVAL_S`` it fetches ``current_prices/futures/rt`` and ``/exchange/ticker``
concurrently (two threads, same instant) and records, per asset:

P10 — basis: perp mark (``mp``) and perp last (``ls``) versus spot last, in bps.
      A sample is VALID only if the two local receive times are within 2 s AND the
      upstream futures tick time ``btST`` is within 2 s of the spot row's ``timestamp``
      (whole seconds). Everything else is discarded and counted.
P9  — ecode divergence: CoinDCX-internal INR markets (``I-X_INR``) converted to USDT via
      ``I-USDT_INR``, versus the relayed ``B-X_USDT``. All three rows come from the SAME
      ticker response, so they are simultaneous by construction.

Raw samples are appended to ``data/probe_results/p09_p10_samples_<stamp>.jsonl`` as they
are taken, so an interrupted run keeps its evidence; the summary is written at the end
(or with ``--summarize <file>``).
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from cdx_http.client import HttpClient
from cdx_http.jsonutil import loads_decimal

from .common import DATA, FUTURES_RT, MARKETS_DETAILS, PAIRS, SPOT_TICKER, ProbeRun, _Enc, iso, setup_logging

INTERVAL_S = 20
TOL_MS = 2000


def _fetch(client: HttpClient, url: str):
    try:
        return client.request("GET", url)
    except Exception as e:  # noqa: BLE001 - a failed sample is recorded, not fatal
        return e


def sample(c1: HttpClient, c2: HttpClient, spot_markets: dict, inr: dict) -> dict:
    with ThreadPoolExecutor(2) as ex:
        f_rt = ex.submit(_fetch, c1, FUTURES_RT)
        f_tk = ex.submit(_fetch, c2, SPOT_TICKER)
        rt, tk = f_rt.result(), f_tk.result()
    s = {"at": iso(time.time_ns() // 1_000_000)}
    if isinstance(rt, Exception) or isinstance(tk, Exception) or not (rt.ok and tk.ok):
        s["error"] = f"rt={getattr(rt, 'status', rt)!s} ticker={getattr(tk, 'status', tk)!s}"
        return s
    prices = loads_decimal(rt.body)["prices"]
    rows = {r["market"]: r for r in loads_decimal(tk.body)}
    s["recv_diff_ms"] = rt.received_at_ms - tk.received_at_ms
    s["assets"] = {}
    usdt_inr = rows.get(inr.get("USDT"))
    for pair in PAIRS:
        asset = pair.split("-")[1].split("_")[0]
        f = prices.get(pair, {})
        sp = rows.get(spot_markets[pair])
        a = {}
        if sp and "mp" in f and "btST" in f:
            spot_last = Decimal(sp["last_price"])
            a.update({
                "spot_last": spot_last, "spot_ts_ms": int(sp["timestamp"]) * 1000,
                "mp": f["mp"], "ls": f.get("ls"), "btST": f["btST"], "bmST": f.get("bmST"), "fr": f.get("fr"),
                "efr": f.get("efr"), "skw": f.get("skw"),
                "basis_mark_bps": (Decimal(f["mp"]) / spot_last - 1) * 10000,
                "basis_last_bps": (Decimal(f["ls"]) / spot_last - 1) * 10000 if "ls" in f else None,
                "upstream_ts_diff_ms": int(f["btST"]) - int(sp["timestamp"]) * 1000,
            })
            a["valid"] = abs(s["recv_diff_ms"]) <= TOL_MS and abs(a["upstream_ts_diff_ms"]) <= TOL_MS
        ir = rows.get(inr.get(asset))
        if ir and usdt_inr and sp:
            implied = Decimal(ir["last_price"]) / Decimal(usdt_inr["last_price"])
            a["p9"] = {"inr_last": ir["last_price"], "usdt_inr_last": usdt_inr["last_price"],
                       "implied_usdt": implied, "b_last": sp["last_price"],
                       "spread_bps": (implied / Decimal(sp["last_price"]) - 1) * 10000,
                       "inr_ts": ir.get("timestamp"), "usdt_inr_ts": usdt_inr.get("timestamp"),
                       "b_ts": sp.get("timestamp"), "inr_volume_24h": ir.get("volume"),
                       "inr_bid": ir.get("bid"), "inr_ask": ir.get("ask")}
        s["assets"][pair] = a
    return s


def _stats(xs: list[float]) -> dict:
    if not xs:
        return {"n": 0}
    xs = sorted(xs)
    q = lambda p: xs[min(len(xs) - 1, int(p * (len(xs) - 1)))]  # noqa: E731
    return {"n": len(xs), "mean": round(statistics.fmean(xs), 3),
            "stdev": round(statistics.stdev(xs), 3) if len(xs) > 1 else None,
            "min": round(xs[0], 3), "p05": round(q(0.05), 3), "median": round(q(0.5), 3),
            "p95": round(q(0.95), 3), "max": round(xs[-1], 3)}


def _corr(a, b):
    if len(a) < 3:
        return None
    try:
        return round(statistics.correlation(a, b), 4)
    except statistics.StatisticsError:
        return None


def summarize(samples_path: Path) -> dict:
    samples = [json.loads(l) for l in samples_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    ok = [s for s in samples if "assets" in s]
    out = {"samples_file": str(samples_path), "attempts": len(samples), "fetch_errors": len(samples) - len(ok),
           "first": samples[0]["at"] if samples else None, "last": samples[-1]["at"] if samples else None,
           "recv_diff_ms": _stats([s["recv_diff_ms"] for s in ok]), "P10": {}, "P9": {}}
    for pair in PAIRS:
        rows = [s["assets"].get(pair, {}) for s in ok]
        rows = [r for r in rows if "valid" in r]
        valid = [r for r in rows if r["valid"]]
        bm = [float(r["basis_mark_bps"]) for r in valid]
        fr = [float(r["fr"]) * 10000 for r in valid if r.get("fr") is not None]
        out["P10"][pair] = {
            "attempts": len(rows), "valid": len(valid),
            "discard_fraction": round(1 - len(valid) / len(rows), 4) if rows else None,
            "upstream_ts_diff_ms_all": _stats([r["upstream_ts_diff_ms"] for r in rows]),
            "basis_mark_bps": _stats(bm),
            "basis_last_bps": _stats([float(r["basis_last_bps"]) for r in valid if r.get("basis_last_bps") is not None]),
            "fr_bps_per_8h": _stats(fr),
            "corr_basis_mark_vs_fr": _corr(bm, fr) if len(fr) == len(bm) else None,
        }
        p9 = [s["assets"].get(pair, {}).get("p9") for s in ok]
        p9 = [x for x in p9 if x]
        out["P9"][pair] = {"samples": len(p9), "spread_bps": _stats([float(x["spread_bps"]) for x in p9]),
                           "inr_volume_24h_last": p9[-1]["inr_volume_24h"] if p9 else None,
                           "max_ts_disagreement_s": max((max(int(x["inr_ts"] or 0), int(x["usdt_inr_ts"] or 0),
                                                             int(x["b_ts"] or 0)) - min(int(x["inr_ts"] or 0),
                                                             int(x["usdt_inr_ts"] or 0), int(x["b_ts"] or 0))
                                                         for x in p9), default=None)}
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=4.5)
    ap.add_argument("--summarize", type=Path)
    args = ap.parse_args()
    setup_logging()
    run = ProbeRun("p09_p10", capture=False)
    if args.summarize:
        run.save(summarize(args.summarize))
        return 0

    details = run.parse(run.get(MARKETS_DETAILS))
    by_pair = {d["pair"]: d for d in details}
    spot_markets = {p: by_pair[p]["coindcx_name"] for p in PAIRS}
    inr = {}
    for asset in ("BTC", "ETH", "SOL", "USDT"):
        d = by_pair.get(f"I-{asset}_INR")
        if d:
            inr[asset] = d["coindcx_name"]
    print("spot markets", spot_markets, "INR markets", inr, flush=True)

    samples_path = DATA / "probe_results" / f"p09_p10_samples_{run.stamp}.jsonl"
    samples_path.parent.mkdir(parents=True, exist_ok=True)
    c1, c2 = HttpClient(default_rate=1.0), HttpClient(default_rate=1.0)
    deadline = time.time() + args.hours * 3600
    n = 0
    with open(samples_path, "a", encoding="utf-8") as fh:
        while time.time() < deadline:
            t0 = time.time()
            s = sample(c1, c2, spot_markets, inr)
            fh.write(json.dumps(s, cls=_Enc, separators=(",", ":")) + "\n")
            fh.flush()
            n += 1
            if n % 30 == 0:
                print(f"{n} samples", flush=True)
            time.sleep(max(0.0, INTERVAL_S - (time.time() - t0)))
    run.save({"market_names": {"spot": spot_markets, "inr": inr}, **summarize(samples_path)})
    return 0


if __name__ == "__main__":
    from .common import run_logged
    sys.exit(run_logged("p09_p10_sampler", main))
