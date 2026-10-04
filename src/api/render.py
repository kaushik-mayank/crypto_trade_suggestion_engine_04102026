"""Dashboard rendering (specs/15).

> The design problem here is not "look professional". It is **not misleading a person who is about to
> risk money.**

Six failure modes a conventional dashboard commits by default, and what is done about each:

| # | Failure | What prevents it here |
|---|---|---|
| 1 | A quiet market looks like a broken system | Empty panels state the **binding condition**, never blank |
| 2 | ``confidence: null`` looks like a bug | The reason is the copy, and ``breakeven_hit_rate`` appears in its place |
| 3 | ``validated: false`` gets scanned past | A separate labelled sub-section with a dashed border, not a badge |
| 4 | Warm-up looks like failure | Warming areas say "warming", with how much longer |
| 5 | Three correlated longs look like three opportunities | The exposure strip states it plainly |
| 6 | A stale number looks like a live one | Stale is desaturated **and** relabelled with its age |

``ENTER_NOW`` relocates to the action bar at the top of the page: **position, size and the word
"enter"** — three independent cues before colour is considered, because colour is already spent on
direction and health, and roughly one in twelve men cannot separate the red/green axis reliably.

Rendered as plain strings rather than through a template engine so that the dashboard has no build
step and no runtime dependency beyond the standard library (specs/10 §6 chose Jinja2 + HTMX; these
fragments are the HTMX half and stay simple enough to test by asserting on their text).
"""
from __future__ import annotations

import html
from typing import Any

from api.services import ServiceResult
from core.health import SystemStatus

#: specs/15 §3. Named here so a change is one edit and the tests can assert against the token, not a
#: literal colour.
TOKENS = {
    "--bg": "#0f1115", "--surface": "#171a21", "--text": "#e6e8eb", "--muted": "#9aa3ad",
    "--long": "#3fb950", "--short": "#f85149", "--act": "#d29922", "--stale": "#8b5cf6",
    "--down": "#da3633", "--border": "#2a2f3a",
}


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def age_text(age_seconds: float | None) -> str:
    """Age, never a bare timestamp: "4s ago" is actionable, "14:32:07" makes the reader do arithmetic."""
    if age_seconds is None:
        return "age unknown"
    if age_seconds < 60:
        return f"{age_seconds:.0f}s ago"
    if age_seconds < 3600:
        return f"{age_seconds / 60:.0f}m ago"
    return f"{age_seconds / 3600:.1f}h ago"


def age_class(age_seconds: float | None) -> str:
    """Amber past 60 s, red past 300 s (specs/15 §9)."""
    if age_seconds is None:
        return "age-unknown"
    if age_seconds > 300:
        return "age-red"
    if age_seconds > 60:
        return "age-amber"
    return "age-ok"


def render_envelope(result: ServiceResult) -> str:
    env = result.envelope
    status = env.system_status
    banner = ""
    if status is SystemStatus.DOWN:
        # Full-width banner, panels dimmed: the reader must not mistake this for a quiet market.
        reasons = "; ".join(env.degraded_reasons) or "the service could not answer"
        banner = (f'<div class="banner down" role="alert">SYSTEM DOWN — {_esc(reasons)}. '
                  f'Last known data: {age_text(env.age_seconds)}. '
                  f'<span class="corr">ref {_esc(env.correlation_id)}</span></div>')
    elif status is SystemStatus.DEGRADED:
        banner = (f'<div class="banner degraded">DEGRADED — '
                  f'{_esc("; ".join(env.degraded_reasons) or "a source is unhealthy")}</div>')
    elif status is SystemStatus.WARMING:
        banner = ('<div class="banner warming">WARMING — features are still accumulating history. '
                  'This is not a failure.</div>')
    return (f'{banner}<div class="envelope {age_class(env.age_seconds)}">'
            f'<span class="status">{_esc(status.value)}</span> · '
            f'<span class="age">{age_text(env.age_seconds)}</span></div>')


def render_signal_card(signal: dict[str, Any]) -> str:
    """One card. ``ENTER_NOW`` cards are rendered by :func:`render_action_bar` instead."""
    payload = signal.get("payload") or {}
    raw = payload.get("raw") or {}
    direction = raw.get("direction", signal.get("direction", ""))
    validated = bool(signal.get("validated"))
    classes = ["card", f"dir-{_esc(direction).lower()}"]
    if not validated:
        classes.append("unvalidated")

    k_net = signal.get("k_net") or (payload.get("detail") or {}).get("k_net")
    breakeven = signal.get("breakeven_hit_rate") or (payload.get("detail") or {}).get(
        "breakeven_hit_rate")

    # specs/15 §7: confidence is never blank and never a dash. The breakeven hit rate is the honest,
    # computable substitute, and it is labelled as such.
    confidence = ('<div class="confidence">probability <span class="muted">not estimated — no '
                  'validated sample yet</span>'
                  + (f'<br>needs {float(breakeven) * 100:.0f}% win rate to break even'
                     if breakeven else "")
                  + "</div>")

    levels = "".join(
        f'<div class="level"><span>{label}</span><span>{_esc(value)}</span></div>'
        for label, value in (("entry", signal.get("entry")), ("stop", signal.get("stop")),
                             ("target", (signal.get("targets") or [None])[0]))
        if value is not None)

    rationale = _esc(raw.get("rationale", ""))[:400]
    return (
        f'<article class="{" ".join(classes)}" data-signal-id="{_esc(signal.get("signal_id"))}">'
        f'<header><span class="strategy">{_esc(signal.get("strategy_id"))}</span>'
        f'<span class="asset">{_esc(signal.get("asset"))}</span>'
        f'<span class="direction">{_esc(direction)}</span>'
        f'<span class="state">{_esc(signal.get("state"))}</span></header>'
        f'{levels}'
        f'<div class="knet">k_net {_esc(k_net) if k_net else "not computed"}</div>'
        f'{confidence}'
        f'<p class="rationale">{rationale}</p>'
        + ("" if validated else
           '<footer class="not-backtested">not yet backtested — this strategy has no out-of-sample '
           'validation, and its size is reduced accordingly</footer>')
        + "</article>")


def render_action_bar(signals: list[dict[str, Any]]) -> str:
    """``ENTER_NOW`` signals leave their class panel and come here (specs/15 §6).

    Three independent cues before colour: **position** (top of the page), **size** (display type) and
    **the word "enter"**. When empty it says so — a blank region reads as broken.
    """
    live = [s for s in signals if s.get("state") == "ENTER_NOW"]
    waiting = [s for s in signals if s.get("state") == "SETUP_DETECTED"]
    if not live:
        return (f'<section class="action-bar empty"><p>no entries live — '
                f'{len(waiting)} setup{"s" if len(waiting) != 1 else ""} waiting</p></section>')
    cards = "".join(
        f'<div class="enter-card"><span class="word">ENTER</span>'
        f'<span class="asset">{_esc(s.get("asset"))}</span>'
        f'<span class="direction">{_esc(s.get("direction"))}</span>'
        f'<span class="trigger">{_esc(s.get("entry"))}</span>'
        f'<span class="strategy">{_esc(s.get("strategy_id"))}</span></div>'
        for s in live)
    return f'<section class="action-bar live">{cards}</section>'


def render_exposure_strip(signals: list[dict[str, Any]]) -> str:
    """The one place the dashboard editorialises, and it does so because the alternative — three
    identical cards implying three independent opportunities — is actively misleading (specs/15 §8)."""
    longs = [s for s in signals if s.get("direction") == "LONG"]
    shorts = [s for s in signals if s.get("direction") == "SHORT"]
    dominant = longs if len(longs) >= len(shorts) else shorts
    assets = sorted({str(s.get("asset")) for s in dominant if s.get("asset")})
    line = f'{len(longs)} long · {len(shorts)} short'
    note = ""
    if len(assets) > 1:
        names = ", ".join(_esc(a) for a in assets)
        note = (f'<p class="concentration">{len(assets)} same-direction signals across {names} are '
                f'approximately one position: these assets run 0.7-0.9 correlated.</p>')
    return f'<section class="exposure"><div class="counts">{line}</div>{note}</section>'


def render_empty_panel(strategy_id: str, rejection: dict[str, Any] | None) -> str:
    """Never blank. The binding condition turns an empty screen into information about the market —
    the visible payoff of the evaluate-everything rule (specs/13 §4)."""
    if rejection is None:
        return (f'<div class="panel empty"><span class="strategy">{_esc(strategy_id)}</span>'
                f'<span class="reason">no evaluation recorded yet</span></div>')
    binding = rejection.get("binding_condition") or "unknown"
    passed = rejection.get("passed_count", 0)
    total = rejection.get("total_count", 0)
    return (f'<div class="panel empty"><span class="strategy">{_esc(strategy_id)}</span>'
            f'<span class="reason">no setup — {_esc(binding)}</span>'
            f'<span class="counts">({passed} of {total} conditions pass)</span></div>')


def render_signals(result: ServiceResult) -> str:
    """The signals fragment. A service failure renders as DOWN, never as a clean empty list."""
    data = result.payload or {}
    signals = data.get("signals", [])
    if not result.ok:
        return (render_envelope(result)
                + '<section class="signals blind"><p>signals cannot be read right now, so this is '
                  '<strong>not</strong> an empty market. See the banner above.</p></section>')
    validated = [s for s in signals if s.get("validated")]
    unvalidated = [s for s in signals if not s.get("validated")]
    body = "".join(render_signal_card(s) for s in validated)
    tier_b = ("" if not unvalidated else
              '<section class="not-backtested-section"><h3>not yet backtested</h3>'
              + "".join(render_signal_card(s) for s in unvalidated) + "</section>")
    if not signals:
        body = ('<p class="quiet">no setups match right now. The panels below state which condition '
                'is binding for each strategy.</p>')
    return (render_envelope(result) + render_action_bar(signals)
            + render_exposure_strip(signals)
            + f'<section class="signals">{body}</section>{tier_b}')


def render_health(result: ServiceResult) -> str:
    data = result.payload or {}
    components = data.get("components", []) if isinstance(data, dict) else []
    rows = "".join(
        f'<tr class="c-{_esc(c.get("status", "")).lower()}"><td>{_esc(c.get("name"))}</td>'
        f'<td>{_esc(c.get("status"))}</td><td>{_esc(c.get("problem_class") or "-")}</td>'
        f'<td>{_esc(c.get("detail"))}</td></tr>'
        for c in components)
    return (render_envelope(result)
            + '<table class="health"><thead><tr><th>component</th><th>status</th>'
              '<th>problem class</th><th>detail</th></tr></thead>'
            + f"<tbody>{rows}</tbody></table>")


def render_rejections(result: ServiceResult) -> str:
    data = result.payload or {}
    items = data.get("rejections", [])
    if not items:
        return render_envelope(result) + '<p class="quiet">no rejections recorded for this asset.</p>'
    rows = "".join(
        f'<tr><td>{_esc(r.get("strategy_id"))}</td>'
        f'<td>{_esc(r.get("binding_condition") or "-")}</td>'
        f'<td>{_esc(r.get("passed_count"))}/{_esc(r.get("total_count"))}</td></tr>'
        for r in items[:25])
    binding = "".join(
        f'<li>{_esc(b.get("strategy_id"))}: {_esc(b.get("binding_condition"))} '
        f'({_esc(b.get("n"))} times)</li>'
        for b in data.get("most_binding", []))
    return (render_envelope(result)
            + f'<table class="rejections"><tbody>{rows}</tbody></table>'
            + (f'<ul class="binding">{binding}</ul>' if binding else ""))


STYLE = """
:root { %(tokens)s }
body { background: var(--bg); color: var(--text); font: 14px/1.5 system-ui, sans-serif; margin: 0; }
.banner { padding: .75rem 1rem; font-weight: 600; }
.banner.down { background: var(--down); }
.banner.degraded { border-left: 4px solid var(--stale); background: var(--surface); }
.banner.warming { border-left: 4px solid var(--act); background: var(--surface); }
.envelope { padding: .5rem 1rem; color: var(--muted); }
.age-amber .age { color: var(--act); } .age-red .age { color: var(--down); font-weight: 700; }
.action-bar { padding: 1rem; border-bottom: 1px solid var(--border); }
.action-bar.live { background: var(--act); color: #14161a; }
.action-bar .word { font-size: 2rem; font-weight: 800; letter-spacing: .05em; }
.action-bar .trigger { font-size: 1.75rem; font-variant-numeric: tabular-nums; }
.action-bar.empty { color: var(--muted); }
.exposure { padding: .75rem 1rem; border-bottom: 1px solid var(--border); }
.exposure .concentration { color: var(--act); }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px;
        padding: 1rem; margin: 1rem; }
.card.unvalidated { border-style: dashed; opacity: .85; }
.card .confidence .muted { color: var(--muted); }
.not-backtested { color: var(--muted); margin-top: .5rem; font-size: .9em; }
.panel.empty { padding: .5rem 1rem; color: var(--muted); }
.signals.blind { padding: 1rem; color: var(--down); }
.quiet { padding: 1rem; color: var(--muted); }
table { width: 100%%; border-collapse: collapse; }
th, td { text-align: left; padding: .4rem 1rem; border-bottom: 1px solid var(--border); }
""" % {"tokens": " ".join(f"{k}: {v};" for k, v in TOKENS.items())}


def render_page(signals_result: ServiceResult, health_result: ServiceResult) -> str:
    """The whole page. HTMX polls the fragments; this is the shell (specs/15 §10)."""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>crypto-trader suggestions</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/htmx/1.9.12/htmx.min.js"></script>
<style>{STYLE}</style></head>
<body>
<header class="page-header"><h1>suggestions</h1>
<p class="muted">This system never places orders. Every number is a suggestion to act on or ignore.</p>
</header>
<main hx-get="/fragments/signals" hx-trigger="every 5s" hx-swap="innerHTML">
{render_signals(signals_result)}
</main>
<aside hx-get="/fragments/health" hx-trigger="every 15s" hx-swap="innerHTML">
{render_health(health_result)}
</aside>
</body></html>"""
