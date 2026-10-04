"""P3 — spot market existence and exact pair strings for BTC / ETH / SOL (all ecodes)."""
from __future__ import annotations

import sys

from .common import MARKETS_DETAILS, ProbeRun, setup_logging

ASSETS = ("BTC", "ETH", "SOL")
FIELDS = ("coindcx_name", "symbol", "pair", "ecode", "status", "base_currency_short_name",
          "target_currency_short_name", "min_notional", "step", "base_currency_precision",
          "target_currency_precision", "min_quantity", "max_quantity", "order_types")


def main() -> int:
    setup_logging()
    run = ProbeRun("p03")
    res = run.get(MARKETS_DETAILS)
    rows = run.parse(res)
    usdt = [{k: r.get(k) for k in FIELDS} for r in rows
            if r.get("target_currency_short_name") in ASSETS and r.get("base_currency_short_name") == "USDT"]
    variants = sorted({(r.get("target_currency_short_name"), r.get("base_currency_short_name"), r.get("ecode"),
                        r.get("pair"), r.get("status")) for r in rows
                       if r.get("target_currency_short_name") in ASSETS})
    ecodes_all = sorted({r.get("ecode") for r in rows})
    run.save({
        "question": "Do spot BTC/ETH/SOL-USDT markets exist; exact pair strings; every ecode variant",
        "total_markets": len(rows),
        "ecodes_in_listing": ecodes_all,
        "usdt_markets": usdt,
        "all_variants_for_assets": [dict(zip(("target", "base", "ecode", "pair", "status"), v)) for v in variants],
        "non_B_usdt_markets": [m for m in usdt if m["ecode"] != "B"],
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
