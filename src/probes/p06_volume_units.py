"""P6 — futures candlestick volume units: base or quote?

Compares one closed 1h futures bar against the same spot hour (spot volume is
verified base-denominated) and against rt ``v`` (verified quote, 24h).
"""
from __future__ import annotations

import sys
from decimal import Decimal

from .common import FUTURES_CANDLES, FUTURES_RT, PAIRS, SPOT_CANDLES, ProbeRun, iso, now_ms, setup_logging

H = 3_600_000


def main() -> int:
    setup_logging()
    run = ProbeRun("p06")
    now = now_ms()
    hour = now - now % H - 2 * H           # a bar closed well before now
    rt = run.parse(run.get(FUTURES_RT))["prices"]
    out = []
    for pair in PAIRS:
        fut = run.parse(run.get(FUTURES_CANDLES, {"pair": pair, "from": (hour - 24 * H) // 1000,
                                                  "to": (hour + H) // 1000, "resolution": "60", "pcode": "f"}))
        fbars = {int(b["time"]): b for b in fut.get("data", [])}
        spot = run.parse(run.get(SPOT_CANDLES, {"pair": pair, "interval": "1h", "startTime": hour - 23 * H,
                                                "endTime": hour, "limit": 24}))
        sbars = {int(b["time"]): b for b in spot}
        fb, sb = fbars.get(hour), sbars.get(hour)
        if not fb or not sb:
            out.append({"pair": pair, "error": "bar missing", "hour": iso(hour)})
            continue
        typ = (Decimal(fb["high"]) + Decimal(fb["low"]) + Decimal(fb["close"])) / 3
        f24 = sum(Decimal(b["volume"]) * Decimal(b["close"]) for t, b in fbars.items() if hour - 23 * H <= t <= hour)
        out.append({
            "pair": pair, "hour": iso(hour),
            "futures_volume": fb["volume"], "futures_volume_x_price": round(Decimal(fb["volume"]) * typ, 2),
            "spot_volume_base": sb["volume"], "typical_price": round(typ, 4),
            "rt_v_24h_quote": rt[pair].get("v"),
            "futures_24h_sum_volume_x_close": round(f24, 0),
            "ratio_rt_v_to_24h_volume_x_close": round(Decimal(str(rt[pair]["v"])) / f24, 3) if f24 else None,
        })
    run.save({
        "question": "Is futures candle volume base- or quote-denominated?",
        "method": "if base, sum(volume*close) over 24h should be ~ rt v (quote, 24h); if quote, volume alone ~ rt v/24",
        "samples": out,
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
