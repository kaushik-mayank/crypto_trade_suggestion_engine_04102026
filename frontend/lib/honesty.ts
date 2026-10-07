/**
 * The display rules from specs/15, mirrored from `src/api/render.py`.
 *
 * **This is a deliberate duplication and it carries a named risk.** The engine already has a
 * server-rendered dashboard whose rules are tested in Python; these are the same rules in
 * TypeScript. Two implementations of one rule can disagree, which is exactly the class of problem
 * this project refuses elsewhere (one ACT, one trail, one cost model).
 *
 * It is accepted here for one reason: the alternative is worse. Rendering HTML from the engine and
 * injecting it into React means `dangerouslySetInnerHTML` over a network boundary, and a dashboard
 * whose markup arrives as a string cannot be type-checked at all. So instead the duplication is
 * confined to **this one file**, it is small enough to read side by side with `render.py`, and
 * `honesty.test.mjs` asserts the same thresholds the Python tests assert.
 *
 * Everything else in the frontend is a thin renderer of what the API's own envelope says. The
 * judgements — is the data fresh, is the system degraded, is a signal validated — are made by the
 * engine and travel in the payload. The frontend's job is to not throw that information away.
 */

/** The six failure modes specs/15 names, as a checklist this file is answerable to. */
export const FAILURE_MODES = [
  'a quiet market looks like a broken system',
  'confidence: null looks like a bug',
  'validated: false gets scanned past',
  'warm-up looks like failure',
  'three correlated longs look like three opportunities',
  'a stale number looks like a live one',
] as const;

/** specs/15 §9. Amber past 60s, red past 300s. Mirrors `render.py:age_class`. */
export const AGE_AMBER_S = 60;
export const AGE_RED_S = 300;

export type AgeState = 'ok' | 'amber' | 'red' | 'unknown';

export function ageState(ageSeconds: number | null | undefined): AgeState {
  if (ageSeconds === null || ageSeconds === undefined || Number.isNaN(ageSeconds)) return 'unknown';
  if (ageSeconds > AGE_RED_S) return 'red';
  if (ageSeconds > AGE_AMBER_S) return 'amber';
  return 'ok';
}

/**
 * Age as elapsed time, never a wall-clock stamp. Mirrors `render.py:age_text`.
 *
 * "4s ago" is actionable; "14:32:07" makes the reader do arithmetic, and a reader doing arithmetic
 * under time pressure gets it wrong.
 */
export function ageText(ageSeconds: number | null | undefined): string {
  if (ageSeconds === null || ageSeconds === undefined || Number.isNaN(ageSeconds)) {
    return 'age unknown';
  }
  if (ageSeconds < 60) return `${Math.round(ageSeconds)}s ago`;
  if (ageSeconds < 3600) return `${Math.round(ageSeconds / 60)}m ago`;
  return `${(ageSeconds / 3600).toFixed(1)}h ago`;
}

export type PanelState =
  /** The engine answered and there is nothing to show. A quiet market. */
  | { kind: 'empty'; reason: string }
  /** The engine could not answer. Not the same thing, and must not look like it. */
  | { kind: 'down'; message: string; likelyCause?: string; remediation?: string; correlationId?: string }
  /** Still accumulating history. Not a failure. */
  | { kind: 'warming'; detail: string }
  | { kind: 'ready' };

/**
 * Decide what a panel is showing, from the API's own envelope.
 *
 * The distinction between `empty` and `down` is the single most important thing this file does.
 * specs/14 §10 calls rendering an unreachable engine as an empty market "the most dangerous thing
 * this API can do", because the reader concludes there are no opportunities when the truth is that
 * nobody knows.
 */
export function panelState(payload: unknown): PanelState {
  if (!payload || typeof payload !== 'object') {
    return { kind: 'down', message: 'the dashboard received no readable response' };
  }
  const p = payload as Record<string, any>;

  if (p.error) {
    return {
      kind: 'down',
      message: String(p.error.message ?? 'the engine reported an error'),
      likelyCause: p.error.likely_cause ?? undefined,
      remediation: p.error.remediation ?? undefined,
      correlationId: p.error.correlation_id ?? undefined,
    };
  }

  const status = String(p.envelope?.system_status ?? p.system_status ?? '').toUpperCase();
  if (status === 'DOWN') {
    return { kind: 'down', message: p.envelope?.detail ?? 'the engine reports itself DOWN' };
  }
  if (status === 'WARMING') {
    return { kind: 'warming', detail: p.envelope?.detail ?? 'accumulating history' };
  }
  return { kind: 'ready' };
}

/**
 * Whether a signal may be shown at all.
 *
 * The engine already filters control strategies out of every response, and the controls already
 * refuse to run outside a backtest. This is the third layer, and it exists because a control signal
 * reaching a person would be indistinguishable from a real suggestion — so each layer is written as
 * if the others are absent.
 */
export function displayable(signal: Record<string, any>): boolean {
  const detail = signal?.detail ?? {};
  if (detail.control) return false;
  if (detail.tradeable === false) return false;
  if (signal?.tradeable === false) return false;
  return true;
}

/**
 * What to print where a confidence score would conventionally go.
 *
 * specs/15 failure mode 2: a blank confidence reads as a bug. The honest replacement is the
 * breakeven hit rate, which says what the trade needs in order to pay rather than guessing how
 * likely it is to.
 */
export function confidenceCopy(signal: Record<string, any>): string {
  const p = signal?.detail?.breakeven_hit_rate ?? signal?.breakeven_hit_rate;
  if (typeof p === 'number' && Number.isFinite(p)) {
    return `needs to work ${(p * 100).toFixed(0)}% of the time to break even`;
  }
  return 'no confidence score is published — nothing here has been validated yet';
}

/** specs/15 failure mode 3: not a badge. The caller renders this in its own bordered section. */
export function validationNotice(signal: Record<string, any>): string | null {
  const validated = signal?.detail?.validated;
  if (validated === true) return null;
  return 'This strategy has never been tested against history. Its size is reduced accordingly, and the suggestion is for observation only.';
}

/**
 * specs/15 failure mode 5. Three simultaneous longs on 0.7–0.9 correlated assets are one leveraged
 * position, and the strip has to say so rather than leaving the reader to count cards.
 */
export function concentrationNotice(longs: number, shorts: number): string | null {
  if (longs >= 2) {
    return `${longs} long suggestions across correlated assets are closer to one leveraged position than to ${longs} opportunities.`;
  }
  if (shorts >= 2) {
    return `${shorts} short suggestions across correlated assets are closer to one leveraged position than to ${shorts} opportunities.`;
  }
  return null;
}
