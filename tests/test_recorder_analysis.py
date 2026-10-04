"""Hand-computed checks for the P14 book metrics."""
import pytest

from probes.recorder_analysis import _book_metrics


def test_book_metrics_hand_computed():
    # bid 99.9 / ask 100.1 -> mid 100.0, spread 0.2/100 = 20 bps
    book = {"bids": {"99.9": "2", "99.8": "1", "99.0": "5"},     # key order deliberately unsorted
            "asks": {"100.3": "1", "100.1": "3", "101.0": "4"}}
    m = _book_metrics(book)
    assert m["spread_bps"] == pytest.approx(20.0)
    assert (m["levels_bid"], m["levels_ask"]) == (3, 3)
    # within 25 bps of 100 -> [99.75, 100.25]: bids 99.9x2 + 99.8x1, asks 100.1x3
    assert m["depth_usdt_25bps"] == pytest.approx(99.9 * 2 + 99.8 * 1 + 100.1 * 3)
    # within 10 bps -> [99.9, 100.1]: 99.9x2 + 100.1x3
    assert m["depth_usdt_10bps"] == pytest.approx(99.9 * 2 + 100.1 * 3)
    # within 50 bps -> [99.5, 100.5]: adds 100.3x1
    assert m["depth_usdt_50bps"] == pytest.approx(99.9 * 2 + 99.8 + 100.1 * 3 + 100.3)
    assert m["book_span_bps"] == pytest.approx((101.0 - 99.0) / 100 * 10000)
    assert m["crossed"] is False
    # reach: min(100-99.0, 101.0-100) / 100 = 100 bps -> all three bands covered
    assert m["reach_bps"] == pytest.approx(100.0)


def test_band_beyond_book_reach_is_none_not_truncated():
    # 50-level BTC-like book: levels only reach +-2 bps of mid
    book = {"bids": {"99.99": "1", "99.98": "1"}, "asks": {"100.01": "1", "100.02": "1"}}
    m = _book_metrics(book)
    assert m["reach_bps"] == pytest.approx(2.0)
    assert m["depth_usdt_10bps"] is None and m["depth_usdt_50bps"] is None
    assert m["depth_usdt_whole_book"] == pytest.approx(99.99 + 99.98 + 100.01 + 100.02)


def test_empty_book_is_invalid_not_zero_liquidity():
    # futures depth=5/100 returns an empty book with HTTP 200 (findings F-014)
    assert _book_metrics({"bids": {}, "asks": {}}) is None
