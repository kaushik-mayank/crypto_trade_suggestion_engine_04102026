"""The Vercel dashboard duplicates `src/api/render.py`'s display rules. These tests check the copies.

There are two dashboards: the server-rendered one the engine serves on the VM, and the Next.js one
Vercel hosts. They apply the **same** specs/15 honesty rules, which means the rules exist twice —
exactly the kind of duplication this project refuses for ACT, for the trail and for the cost model.

It is accepted for the frontend because the alternative is worse (shipping HTML strings across a
network boundary into `dangerouslySetInnerHTML`, which cannot be type-checked at all), but accepted
duplication still has to be *checked*. Node is not installed in this environment, so the TypeScript
cannot be executed here; instead these tests read the TypeScript as text and assert that its
constants and thresholds match the Python the engine actually uses.

That makes the check run in the suite that runs on every change, rather than in a toolchain that may
not be present. If the two ever disagree, one of these fails and names which file to fix.
"""
import re
from pathlib import Path

import pytest

from api import render

FRONTEND = Path(__file__).resolve().parents[1] / "frontend"
HONESTY_TS = FRONTEND / "lib" / "honesty.ts"
GLOBALS_CSS = FRONTEND / "app" / "globals.css"
PROXY_TS = FRONTEND / "app" / "api" / "proxy" / "[...path]" / "route.ts"


def _require(path: Path) -> str:
    if not path.exists():
        pytest.fail(f"{path.relative_to(FRONTEND.parent)} is missing; the Vercel dashboard needs it")
    return path.read_text(encoding="utf-8")


def _without_comments(source: str) -> str:
    """Strip JS/TS comments, so a rule can be asserted against *code* rather than against prose.

    Needed because the files deliberately explain the hazards they avoid — ``route.ts`` says in a
    comment why a ``NEXT_PUBLIC_`` variable would be wrong — and a naive substring check would read
    the explanation as the offence. The first version of these tests did exactly that.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"(?<!:)//.*", "", source)


def _number(source: str, name: str) -> float:
    match = re.search(rf"export const {name}\s*=\s*([0-9._]+)\s*;", source)
    if match is None:
        pytest.fail(f"{name} is not exported from honesty.ts; the Python side relies on it existing")
    return float(match.group(1))


# ---- the thresholds must agree --------------------------------------------------------------

def test_the_age_thresholds_are_the_same_in_both_dashboards():
    """specs/15 §9: amber past 60 s, red past 300 s.

    Asserted against ``render.py:age_class``'s actual behaviour rather than against a literal, so
    this test follows the Python if the Python changes — and then fails, which is the point.
    """
    source = _require(HONESTY_TS)
    amber = _number(source, "AGE_AMBER_S")
    red = _number(source, "AGE_RED_S")

    # Derive the Python thresholds by probing the function. If someone edits render.py, these move
    # and the comparison below catches the frontend being left behind.
    assert render.age_class(amber) == "age-ok"
    assert render.age_class(amber + 1) == "age-amber"
    assert render.age_class(red) == "age-amber"
    assert render.age_class(red + 1) == "age-red"


def test_an_unknown_age_is_its_own_state_in_both():
    """The dangerous default: if an absent age rendered as fresh, a dashboard with a broken clock
    would look perfectly healthy."""
    assert render.age_class(None) == "age-unknown"
    assert render.age_text(None) == "age unknown"
    source = _require(HONESTY_TS)
    assert "'unknown'" in source and "'age unknown'" in source


def test_age_is_elapsed_time_in_both_and_never_a_timestamp():
    """"4s ago" is actionable; "14:32:07" makes the reader do arithmetic."""
    for seconds in (4, 90, 7200, 123456):
        text = render.age_text(seconds)
        assert "ago" in text
        assert not re.search(r"\d:\d", text), f"{seconds} rendered as a clock time: {text}"
    source = _require(HONESTY_TS)
    assert "ago" in source
    assert "toFixed(1)}h ago" in source, "the hour branch must match render.py's one decimal place"


def test_the_colour_tokens_are_identical_so_a_colour_cannot_mean_two_things():
    """A colour that means "short" on one dashboard and "stale" on the other is worse than no
    colour at all, because the reader has learned the wrong thing on one of them."""
    css = _require(GLOBALS_CSS)
    for name, value in render.TOKENS.items():
        assert f"{name}: {value};" in css, (
            f"{name} is {value} in render.py but not in frontend/app/globals.css"
        )


# ---- the firewall must exist on the frontend too --------------------------------------------

def test_the_frontend_refuses_to_display_a_control_strategy():
    """The third firewall layer. The engine filters controls out of every response and the controls
    refuse to run outside a backtest; this one is written as if neither did, because a control
    signal reaching a person would be indistinguishable from a real suggestion."""
    source = _require(HONESTY_TS)
    assert "export function displayable" in source
    assert "detail.control" in source, "a signal flagged as a control must be refused"
    assert "tradeable === false" in source, "a non-tradeable signal must be refused"


def test_the_frontend_never_claims_a_confidence_value():
    """``confidence`` is null system-wide until Phase 16 (specs/09 §14), and specs/15 failure mode 2
    says a blank reads as a bug — so the absence is explained, in copy."""
    source = _require(HONESTY_TS)
    assert "breakeven_hit_rate" in source
    assert "no confidence score is published" in source
    # And nothing may invent one.
    assert not re.search(r"confidence\s*[:=]\s*[0-9]", source)


def test_the_frontend_distinguishes_cannot_tell_from_nothing_to_show():
    """specs/14 §10 names rendering an unreachable engine as an empty market the most dangerous
    thing this API can do."""
    source = _require(HONESTY_TS)
    assert "'down'" in source and "'empty'" in source
    assert "'warming'" in source, "warm-up is a third state, not a failure"
    panels = _require(FRONTEND / "components" / "Panels.tsx")
    assert "Cannot tell" in panels
    assert "not &ldquo;no opportunities&rdquo;" in panels or "not \u201cno opportunities\u201d" in panels


# ---- the proxy is what keeps the token off the browser --------------------------------------

def test_the_shared_token_is_never_exposed_to_the_browser():
    """A ``NEXT_PUBLIC_`` variable is compiled into the JavaScript the browser downloads.

    The token is read only inside the proxy route, which runs on Vercel's server. If it ever gained
    that prefix, anyone who opened the page could read the credential that reaches the engine.
    """
    proxy = _require(PROXY_TS)
    assert "process.env.CT_API_TOKEN" in proxy
    for path in (PROXY_TS, FRONTEND / "lib" / "api.ts", FRONTEND / "app" / "page.tsx",
                 FRONTEND / "components" / "Panels.tsx"):
        code = _without_comments(_require(path))
        assert "NEXT_PUBLIC_" not in code, (
            f"{path.name} reads a NEXT_PUBLIC_ variable, which is compiled into the JavaScript the "
            "browser downloads; the engine token must never be one"
        )
    # The template must not suggest one either: a name copied from an example is a name nobody
    # re-reads.
    env_lines = [ln for ln in _require(FRONTEND / ".env.example").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
    assert not any("NEXT_PUBLIC_" in ln for ln in env_lines)


def test_the_proxy_forwards_only_get_and_only_an_allow_list():
    """An allow-list rather than a pattern: ``api/**`` would quietly expose whatever gets added to
    the engine later, and the person adding it would have no reason to look at this file."""
    proxy = _require(PROXY_TS)
    assert "export async function GET" in proxy
    for verb in ("POST", "PUT", "PATCH", "DELETE"):
        assert f"export async function {verb}" not in proxy, (
            f"the proxy exports {verb}; the engine is read-only and the proxy must be too"
        )
    assert "const ALLOWED = new Set([" in proxy
    assert "function permitted" in proxy


def test_every_route_the_frontend_asks_for_exists_in_the_engine():
    """A path on the allow-list that the engine does not serve is a panel that will always be DOWN,
    and it would look like an outage rather than a typo."""
    proxy = _require(PROXY_TS)
    allowed = re.findall(r"'(api/[a-z0-9/_-]+)'", proxy)
    app_source = (Path(__file__).resolve().parents[1] / "src" / "api" / "app.py").read_text(
        encoding="utf-8")
    routes = set(re.findall(r'@app\.get\("(/api/[^"]*)"', app_source))
    declared = {r.strip("/") for r in routes}
    for path in allowed:
        assert path in declared, f"the proxy allows {path}, which the engine does not serve"


def test_the_dashboard_states_that_the_system_never_trades():
    """Stated on every load rather than in an about page: someone reading under time pressure
    should not have to remember what the system is permitted to do."""
    page = _require(FRONTEND / "app" / "page.tsx")
    assert "never places" in page
    assert "Read-only" in page


def test_nothing_in_the_frontend_is_cached_at_the_edge():
    """A cached panel is a stale panel presented as current, which is the one thing specs/15 forbids
    outright. Vercel caches aggressively by default, so this has to be switched off deliberately."""
    config = _require(FRONTEND / "next.config.mjs")
    assert "no-store" in config
    proxy = _require(PROXY_TS)
    assert "'no-store'" in proxy
    assert "force-dynamic" in proxy
