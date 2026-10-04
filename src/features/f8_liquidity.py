"""Family 8 — liquidity and execution quality (specs/02, **Tier B, risk engine only**).

``LIQ_IMBAL`` is **explicitly demoted and must not be used as alpha.** Book imbalance carries real
information on a seconds-to-minutes horizon and essentially none on a one-hour-to-daily one. It exists
here as a health and sanity check. That is a rejection on *horizon* grounds, not data grounds — the
data is fine, the timescale is wrong — so the value ships with ``alpha_use: forbidden`` in its detail
and the risk engine is its only consumer.

**The depth bands are narrower than the spec assumes.** Measured (F-032): the futures book is capped
at **50 levels spanning ±0.74 bps** — requesting 100, 200 or 500 returns HTTP 200 with an *empty* book
— and the spot book reaches ±1.79 bps at 100 levels. The "notional within 25 bps" the spec's veto is
written against is therefore not observable at this venue by a factor of roughly 14.

So each band is either measured or reported as ``None`` with the book's actual reach beside it
(decision D-9), and the scalar is the visible book's own notional. Summing what exists and labelling it
"25 bps" would understate true depth and read identically for 10, 25 and 50 bps — three different
questions answered with one number.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from features.numerics import Degenerate, require_finite
from features.spec import NO_HORIZON, ComputeContext, FeatureSpec, NotApplicable

PIT = (None,)

#: Bands to report, in bps from mid. The narrow ones are measurable on this venue; the wide ones are
#: usually beyond the book's reach and are reported as unmeasurable rather than approximated (F-025).
BANDS_BPS: tuple[int, ...] = (1, 2, 5, 10, 25, 50)
#: The band ``LIQ_IMBAL`` is defined over (specs/02 Family 8).
IMBAL_BAND_BPS = 25


def _book(ctx: ComputeContext, source: str) -> Any:
    if ctx.snapshot is None:
        raise NotApplicable("no snapshot attached, so the book cannot be read")
    return ctx.snapshot.tier_b(source, ctx.asset)


def _reach_bps(book: Any) -> tuple[float, float]:
    """How far the visible book extends either side of mid, in bps. The honesty check for every band."""
    mid = book.mid
    bid_reach = float((mid - book.bids[-1][0]) / mid * Decimal(10_000))
    ask_reach = float((book.asks[-1][0] - mid) / mid * Decimal(10_000))
    return bid_reach, ask_reach


def _notional_within(levels: tuple[tuple[Decimal, Decimal], ...], mid: Decimal, bps: int,
                     *, is_bid: bool) -> Decimal:
    limit = mid * (Decimal(1) - Decimal(bps) / Decimal(10_000)) if is_bid else \
        mid * (Decimal(1) + Decimal(bps) / Decimal(10_000))
    total = Decimal(0)
    for price, qty in levels:
        inside = price >= limit if is_bid else price <= limit
        if not inside:
            break
        total += price * qty
    return total


def liq_spread(ctx: ComputeContext) -> float:
    """``10000 * (ask - bid) / mid`` at L1, from the futures book."""
    book = _book(ctx, "futures_book")
    bps = float(book.spread_bps)
    if bps < 0:
        raise Degenerate("CROSSED_BOOK", f"spread is negative ({bps:.3f} bps)")
    ctx.detail = {"ts_ms": book.ts_ms, "best_bid": float(book.bids[0][0]),
                  "best_ask": float(book.asks[0][0]), "mid": float(book.mid),
                  "levels": len(book.bids) + len(book.asks)}
    return require_finite(bps, what="LIQ_SPREAD")


def liq_depth(ctx: ComputeContext) -> float:
    """Cumulative notional, by band where measurable and **as the whole visible book** for the scalar.

    Measured on this venue (F-032): the futures book is capped at 50 levels spanning about ±0.75 bps —
    depths 100/200/500 return HTTP 200 with an *empty* book — and the spot book reaches ±1.8 bps at 100
    levels. So none of the spec's 10/25/50 bps bands is observable here, and even 1 bps is marginal on
    futures.

    The scalar is therefore the notional of the **entire visible book**, paired with
    ``visible_reach_bps`` so it is never mistaken for a fixed-band figure: an honest measurement over a
    stated range beats a 25 bps label on a 0.75 bps sample. The bands stay in the detail, ``None`` where
    the book does not reach, which is what the risk engine needs in order to refuse a size it cannot
    justify (specs/08 §7.1 + D-9).
    """
    book = _book(ctx, "futures_book")
    mid = book.mid
    bid_reach, ask_reach = _reach_bps(book)
    covered = min(bid_reach, ask_reach)

    bands: dict[str, Any] = {}
    widest_measured: int | None = None
    for bps in BANDS_BPS:
        if bps > covered:
            bands[f"{bps}bps"] = None          # the book does not reach this far: not zero, unknown
            continue
        bid_notional = _notional_within(book.bids, mid, bps, is_bid=True)
        ask_notional = _notional_within(book.asks, mid, bps, is_bid=False)
        bands[f"{bps}bps"] = float(bid_notional + ask_notional)
        widest_measured = bps

    visible = float(sum(p * q for p, q in book.bids) + sum(p * q for p, q in book.asks))
    ctx.detail = {"ts_ms": book.ts_ms, "bands": bands, "widest_measured_bps": widest_measured,
                  "visible_notional": visible,
                  "visible_reach_bps": {"bid": round(bid_reach, 3), "ask": round(ask_reach, 3)},
                  "levels": {"bids": len(book.bids), "asks": len(book.asks)},
                  "unmeasurable": [b for b, v in bands.items() if v is None],
                  "scalar_is": "notional of the entire visible book, over visible_reach_bps",
                  "note": "a None band means the visible book does not reach that far (F-032), not "
                          "that depth there is zero; the 25 bps veto of specs/08 §7.1 is not "
                          "observable at this venue"}
    if visible <= 0:
        raise Degenerate("ZERO_NOTIONAL",
                         f"the visible book carries no notional at all ({len(book.bids)} bid and "
                         f"{len(book.asks)} ask levels)")
    return require_finite(visible, what="LIQ_DEPTH")


def liq_imbal(ctx: ComputeContext) -> float:
    """``(bid_notional - ask_notional) / (bid + ask)`` within 25 bps — **health check only.**

    Not alpha at this horizon (specs/02 Family 8). Where the book does not reach 25 bps, the whole
    visible book is used and the detail says so, because an imbalance over a narrower band is still a
    valid health check as long as the band is stated.
    """
    book = _book(ctx, "futures_book")
    mid = book.mid
    bid_reach, ask_reach = _reach_bps(book)
    covered = min(bid_reach, ask_reach)
    band = min(IMBAL_BAND_BPS, max(1, int(covered) if covered >= 1 else 1))
    whole_book = covered < 1
    bid_notional = (sum(p * q for p, q in book.bids) if whole_book
                    else _notional_within(book.bids, mid, band, is_bid=True))
    ask_notional = (sum(p * q for p, q in book.asks) if whole_book
                    else _notional_within(book.asks, mid, band, is_bid=False))
    total = bid_notional + ask_notional
    if total <= 0:
        raise Degenerate("ZERO_NOTIONAL", f"no notional within +/-{band} bps of mid")
    ctx.detail = {"ts_ms": book.ts_ms, "band_bps": band, "requested_band_bps": IMBAL_BAND_BPS,
                  "measured_over": "the entire visible book" if whole_book else f"+/-{band} bps",
                  "visible_reach_bps": round(covered, 3),
                  "bid_notional": float(bid_notional), "ask_notional": float(ask_notional),
                  "alpha_use": "forbidden -- horizon mismatch (specs/02 Family 8)",
                  "consumer": "risk engine and health only"}
    return require_finite(float((bid_notional - ask_notional) / total), what="LIQ_IMBAL")


def liq_spread_spot(ctx: ComputeContext) -> float:
    """The spot book's spread, kept separate from the futures one: they are different instruments with
    different liquidity, and averaging them would describe neither."""
    book = _book(ctx, "spot_book")
    ctx.detail = {"ts_ms": book.ts_ms, "mid": float(book.mid), "levels": len(book.bids) + len(book.asks)}
    return require_finite(float(book.spread_bps), what="LIQ_SPREAD_SPOT")


SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(name="LIQ_SPREAD", version="1.0.0", family=8, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_book",), compute=liq_spread,
                description="futures L1 spread in bps"),
    FeatureSpec(name="LIQ_SPREAD_SPOT", version="1.0.0", family=8, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("spot_book",), compute=liq_spread_spot,
                description="spot L1 spread in bps"),
    FeatureSpec(name="LIQ_DEPTH", version="1.0.0", family=8, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_book",), compute=liq_depth,
                description="cumulative notional by bps band; unreachable bands report None (D-9)"),
    FeatureSpec(name="LIQ_IMBAL", version="1.0.0", family=8, tier="B", timeframes=PIT,
                horizons=NO_HORIZON, windowless=True, tier_b_sources=("futures_book",), compute=liq_imbal,
                description="book imbalance -- health check only, never alpha at this horizon"),
)
