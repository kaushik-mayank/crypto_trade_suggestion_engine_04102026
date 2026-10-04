"""PHASE-26 — the backtest harness (specs/09).

Four parts, kept separate because they fail differently:

* :mod:`backtest.fills` — the fill and cost model (specs/09 §3). The conservative rules, stated
  unconditionally so they cannot be relaxed per bar.
* :mod:`backtest.accounting` — what counts as a trade (specs/09 §4), including the handoff case that
  must span two strategies without becoming two trades.
* :mod:`backtest.metrics` — the required statistics (specs/09 §5), with the ones the spec calls
  *insufficient* marked as such in the output rather than only in the document.
* :mod:`backtest.protocol` — the pre-registration lock file (specs/09 §1), which must exist and
  match before a run is allowed to produce results.
"""
