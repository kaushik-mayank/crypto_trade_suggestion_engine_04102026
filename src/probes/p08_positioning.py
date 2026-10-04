"""P8 — ``data/stats`` reachability and shape.

Part A (no credentials, always runs): unauthenticated GET and POST; empty/unknown pair.
Part B (only if ``.env`` has COINDCX_API_KEY/SECRET): every documented signed combination
  method ∈ {GET (sample code: signed body on GET), POST (HTTP request line)}
  timestamp ∈ {ms, s} (docs contradict themselves), pair ∈ {query, query + body}
  and a field-by-field comparison of the signed and unsigned ``position`` payloads.

Read-only market-statistics endpoint. Never calls anything that trades (invariant 11).
"""
from __future__ import annotations

import itertools
import json
import sys

from cdx_http.signed import load_credentials, signed_request

from .common import FUTURES_STATS, PAIRS, ROOT, ProbeRun, setup_logging


def _field_report(obj) -> dict:
    out = {"top_level_keys": sorted(obj) if isinstance(obj, dict) else type(obj).__name__}
    pos = obj.get("position") if isinstance(obj, dict) else None
    out["position_keys"] = sorted(pos) if isinstance(pos, dict) else None
    for k in ("count_percent", "value_percent"):
        v = pos.get(k) if isinstance(pos, dict) else None
        out[f"position.{k}"] = v
        out[f"position.{k}_populated"] = isinstance(v, dict) and {"long", "short"} <= set(v)
        if out[f"position.{k}_populated"]:
            out[f"position.{k}_sums_to_100"] = abs(float(v["long"]) + float(v["short"]) - 100) < 0.02
    return out


def _json(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def main() -> int:
    setup_logging()
    run = ProbeRun("p08", rate=1.0)
    creds = load_credentials(ROOT / ".env")

    def call(method, pair, headers=None, body=None):
        run.n_requests += 1
        res = run.http.request(method, FUTURES_STATS, params={"pair": pair} if pair else None,
                               headers=headers, body=body)
        text = res.body.decode("utf-8", errors="replace")
        if creds:
            for s in creds.secrets():
                text = text.replace(s, "<redacted>")
        return res, text

    # ---- Part A ----
    unauth = {}
    for pair in PAIRS:
        res, text = call("GET", pair)
        unauth[pair] = {"status": res.status, "body": text, "fields": _field_report(_json(text))}
    res_post, text_post = call("POST", "B-BTC_USDT")
    res_none, text_none = call("GET", None)
    res_bad, text_bad = call("GET", "B-NOPE_USDT")
    part_a = {
        "unauthenticated_GET": unauth,
        "unauthenticated_POST_B-BTC_USDT": {"status": res_post.status, "body": text_post[:300]},
        "GET_without_pair": {"status": res_none.status, "body": text_none[:300]},
        "GET_unknown_pair": {"status": res_bad.status, "body": text_bad[:300]},
    }
    a_pass = all(v["status"] == 200 and v["fields"]["position.count_percent_populated"]
                 and v["fields"]["position.value_percent_populated"] for v in unauth.values())

    # ---- Part B ----
    part_b = None
    if creds:
        combos, working = [], None
        for method, unit, in_body in itertools.product(("GET", "POST"), ("ms", "s"), (False, True)):
            body, headers = signed_request(creds, {"pair": "B-BTC_USDT"} if in_body else None, unit)
            res, text = call(method, "B-BTC_USDT", headers, body)
            combos.append({"method": method, "timestamp_unit": unit, "pair_in_body": in_body,
                           "status": res.status, "body": text[:2000]})
            if res.status == 200 and working is None:
                working = (method, unit, in_body)
        compare = {}
        if working:
            for pair in PAIRS:
                body, headers = signed_request(creds, {"pair": pair} if working[2] else None, working[1])
                _, st = call(working[0], pair, headers, body)
                _, ut = call("GET", pair)
                s_obj, u_obj = _json(st), _json(ut)
                compare[pair] = {
                    "same_keys": _field_report(s_obj)["top_level_keys"] == _field_report(u_obj)["top_level_keys"],
                    "same_position_keys": _field_report(s_obj)["position_keys"] == _field_report(u_obj)["position_keys"],
                    "position_equal": (s_obj or {}).get("position") == (u_obj or {}).get("position"),
                    "signed_extra_top_level_keys": sorted(set(s_obj or {}) - set(u_obj or {})),
                }
        part_b = {"combinations": combos,
                  "working_signed_combination": dict(zip(("method", "timestamp_unit", "pair_in_body"), working))
                  if working else None,
                  "signed_vs_unsigned": compare}

    # Acceptance: zero credentials anywhere in data/.
    scrubbed = 0
    if creds and run.capture_dir and run.capture_dir.exists():
        for f in run.capture_dir.iterdir():
            raw = f.read_bytes()
            new = raw
            for s in creds.secrets():
                new = new.replace(s.encode(), b"<redacted>")
            if new != raw:
                f.write_bytes(new)
                scrubbed += 1

    run.save({
        "question": "Does data/stats work, with which method, and does it need credentials?",
        "part_a_unauthenticated": part_a,
        "part_a_pass": a_pass,
        "part_b_signed": part_b if part_b else "SKIPPED: no credentials in .env",
        "capture_files_scrubbed_of_credentials": scrubbed,
    })
    return 0 if a_pass else 1


if __name__ == "__main__":
    sys.exit(main())
