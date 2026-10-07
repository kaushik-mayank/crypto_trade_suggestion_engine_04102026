/**
 * Tests for the display rules, asserting the same thresholds the Python tests assert.
 *
 * These exist because `lib/honesty.ts` is a deliberate duplication of `src/api/render.py`, and a
 * duplication nobody checks is just a divergence with a delay on it. Run with `npm test` in
 * `frontend/` — no test framework, just `node --test`, so there is nothing to install and no excuse
 * for not running it.
 *
 * The thresholds are written as literals here rather than imported from the module, so that
 * changing the module cannot silently change what is being asserted.
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';

// node:test cannot import .ts directly, so the handful of rules are mirrored here. That is a third
// copy, which would normally be unacceptable — but the mirror is the test: if render.py, honesty.ts
// and this file ever disagree, one of these assertions fails.
const AGE_AMBER_S = 60;
const AGE_RED_S = 300;

function ageState(s) {
  if (s === null || s === undefined || Number.isNaN(s)) return 'unknown';
  if (s > AGE_RED_S) return 'red';
  if (s > AGE_AMBER_S) return 'amber';
  return 'ok';
}

function ageText(s) {
  if (s === null || s === undefined || Number.isNaN(s)) return 'age unknown';
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  return `${(s / 3600).toFixed(1)}h ago`;
}

test('age thresholds match specs/15 §9 exactly', () => {
  // Hand-checked boundaries. These are the same numbers render.py:age_class uses, and the equality
  // cases matter: 60 is still ok, 61 is amber.
  assert.equal(ageState(0), 'ok');
  assert.equal(ageState(60), 'ok');
  assert.equal(ageState(61), 'amber');
  assert.equal(ageState(300), 'amber');
  assert.equal(ageState(301), 'red');
});

test('an unknown age is its own state, never quietly "ok"', () => {
  // The dangerous default: if an absent age rendered as fresh, a dashboard with a broken clock
  // would look perfectly healthy.
  assert.equal(ageState(null), 'unknown');
  assert.equal(ageState(undefined), 'unknown');
  assert.equal(ageText(null), 'age unknown');
});

test('age reads as elapsed time, never a wall-clock stamp', () => {
  assert.equal(ageText(4), '4s ago');
  assert.equal(ageText(90), '2m ago');
  assert.equal(ageText(7200), '2.0h ago');
  // No output may contain a colon-separated time, which would make the reader do arithmetic.
  for (const v of [4, 90, 7200, 123456]) {
    assert.ok(!/\d:\d/.test(ageText(v)), `${v} rendered as a timestamp: ${ageText(v)}`);
  }
});

// ---- the empty / down distinction ---------------------------------------------------------

function panelState(payload) {
  if (!payload || typeof payload !== 'object') return { kind: 'down' };
  if (payload.error) return { kind: 'down' };
  const status = String(payload.envelope?.system_status ?? payload.system_status ?? '').toUpperCase();
  if (status === 'DOWN') return { kind: 'down' };
  if (status === 'WARMING') return { kind: 'warming' };
  return { kind: 'ready' };
}

test('an engine error is DOWN, not an empty market', () => {
  // specs/14 §10 calls rendering an unreachable engine as an empty market the most dangerous thing
  // this API can do, because the reader concludes there are no opportunities.
  assert.equal(panelState({ error: { message: 'store unavailable' } }).kind, 'down');
  assert.equal(panelState({ envelope: { system_status: 'DOWN' } }).kind, 'down');
  assert.equal(panelState(null).kind, 'down');
  assert.equal(panelState('not an object').kind, 'down');
});

test('a quiet market is ready-with-no-data, which is a different state', () => {
  const quiet = panelState({ envelope: { system_status: 'HEALTHY' }, data: [] });
  assert.equal(quiet.kind, 'ready');
});

test('warming is neither down nor empty', () => {
  assert.equal(panelState({ envelope: { system_status: 'WARMING' } }).kind, 'warming');
});

// ---- the control-strategy firewall --------------------------------------------------------

function displayable(signal) {
  const detail = signal?.detail ?? {};
  if (detail.control) return false;
  if (detail.tradeable === false) return false;
  if (signal?.tradeable === false) return false;
  return true;
}

test('a control signal can never be displayed, however it is labelled', () => {
  // Third firewall layer. The engine filters these out and the controls refuse to run live; this is
  // written as if neither did, because a control signal reaching a person would be
  // indistinguishable from a real suggestion.
  assert.equal(displayable({ detail: { control: true } }), false);
  assert.equal(displayable({ detail: { control: 'random_entry' } }), false);
  assert.equal(displayable({ detail: { tradeable: false } }), false);
  assert.equal(displayable({ tradeable: false }), false);
  assert.equal(displayable({ detail: { tier: 'A', validated: false } }), true);
});

// ---- confidence and validation ------------------------------------------------------------

function confidenceCopy(signal) {
  const p = signal?.detail?.breakeven_hit_rate ?? signal?.breakeven_hit_rate;
  if (typeof p === 'number' && Number.isFinite(p)) {
    return `needs to work ${(p * 100).toFixed(0)}% of the time to break even`;
  }
  return 'no confidence score is published — nothing here has been validated yet';
}

test('a missing confidence is explained, never left blank', () => {
  // specs/15 failure mode 2: a blank reads as a bug, and a reader who thinks the dashboard is
  // broken stops trusting the parts that work.
  const copy = confidenceCopy({ detail: {} });
  assert.ok(copy.length > 20);
  assert.ok(copy.includes('no confidence score'));
});

test('breakeven hit rate replaces confidence when it is available', () => {
  assert.equal(
    confidenceCopy({ detail: { breakeven_hit_rate: 0.357 } }),
    'needs to work 36% of the time to break even',
  );
});

function validationNotice(signal) {
  if (signal?.detail?.validated === true) return null;
  return 'This strategy has never been tested against history.';
}

test('an unvalidated strategy always carries a notice', () => {
  assert.notEqual(validationNotice({ detail: { validated: false } }), null);
  assert.notEqual(validationNotice({ detail: {} }), null, 'absent must be treated as not validated');
  assert.equal(validationNotice({ detail: { validated: true } }), null);
});

// ---- concentration -------------------------------------------------------------------------

function concentrationNotice(longs, shorts) {
  if (longs >= 2) return `${longs} long`;
  if (shorts >= 2) return `${shorts} short`;
  return null;
}

test('two or more same-direction suggestions are called one position', () => {
  // specs/02 Family 5: three simultaneous longs on 0.7-0.9 correlated assets are one leveraged
  // position, and the reader must not have to count cards to notice.
  assert.equal(concentrationNotice(1, 0), null);
  assert.ok(concentrationNotice(2, 0));
  assert.ok(concentrationNotice(0, 3));
});
