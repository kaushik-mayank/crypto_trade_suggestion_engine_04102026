'use client';

/**
 * The panels, and the states a panel can be in.
 *
 * Every component here exists to keep one of the specs/15 failure modes from happening. They are
 * small on purpose: all the judgement lives in `lib/honesty.ts` and in the engine's own payload, so
 * these are renderers, not deciders.
 */
import { ageState, ageText, concentrationNotice, confidenceCopy, displayable, validationNotice, type PanelState } from '@/lib/honesty';

/** Age, always as text and colour together — never colour alone. */
export function Freshness({ ageSeconds, label = 'data' }: { ageSeconds?: number | null; label?: string }) {
  const state = ageState(ageSeconds);
  return (
    <span className={`age age-${state}`}>
      {label} {ageText(ageSeconds)}
      {state === 'red' && ' — too old to act on'}
      {state === 'amber' && ' — getting stale'}
      {state === 'unknown' && ' — the engine did not say how old this is'}
    </span>
  );
}

/**
 * The engine could not answer. specs/14 §10: rendering this as an empty market is the most
 * dangerous thing this dashboard could do, so it looks nothing like one.
 */
export function Down({ state }: { state: Extract<PanelState, { kind: 'down' }> }) {
  return (
    <div className="card down">
      <h3>Cannot tell — the engine did not answer</h3>
      <p style={{ margin: '0 0 8px' }}>{state.message}</p>
      {state.likelyCause && (
        <p style={{ margin: '0 0 4px' }}>
          <span className="label">likely cause</span>
          {state.likelyCause}
        </p>
      )}
      {state.remediation && (
        <p style={{ margin: '0 0 4px' }}>
          <span className="label">what to do</span>
          {state.remediation}
        </p>
      )}
      {state.correlationId && <p className="mono" style={{ margin: 0 }}>id {state.correlationId}</p>}
      <p style={{ margin: '8px 0 0', fontWeight: 600 }}>
        This is not &ldquo;no opportunities&rdquo;. Nothing on this page is known to be current.
      </p>
    </div>
  );
}

/** specs/15 failure mode 4: warm-up is not failure, and it says how much longer. */
export function Warming({ detail }: { detail: string }) {
  return (
    <div className="card warming">
      <strong>Warming up</strong>
      <p style={{ margin: '6px 0 0' }}>{detail}</p>
      <p style={{ margin: '6px 0 0', color: 'var(--muted)' }}>
        Nothing is wrong. The engine is still accumulating the history its longest measurements need.
      </p>
    </div>
  );
}

/** specs/15 failure mode 1: a quiet market states the binding condition, never a blank box. */
export function Empty({ reason }: { reason: string }) {
  return (
    <div className="card empty">
      <strong style={{ color: 'var(--text)' }}>No suggestions right now</strong>
      <p style={{ margin: '6px 0 0' }}>{reason}</p>
      <p style={{ margin: '6px 0 0' }}>
        A quiet market is the normal state. The engine is working and has nothing to say.
      </p>
    </div>
  );
}

type Signal = Record<string, any>;

/** specs/15: ENTER_NOW is relocated to the top, with three independent cues before colour. */
export function ActionBar({ signals }: { signals: Signal[] }) {
  const now = signals.filter((s) => String(s.lifecycle).toUpperCase() === 'ENTER_NOW');
  if (now.length === 0) return null;
  return (
    <>
      {now.map((s) => (
        <div className="banner" key={s.signal_id}>
          ENTER NOW — {s.asset} {String(s.direction).toUpperCase()} at {s.entry ?? '—'}
          {s.sizing?.size ? ` · size ${s.sizing.size}` : ''}
        </div>
      ))}
    </>
  );
}

export function SignalCard({ signal }: { signal: Signal }) {
  // The third firewall layer. The engine filters controls out and the controls refuse to run live;
  // this is written as if neither did.
  if (!displayable(signal)) return null;

  const dir = String(signal.direction ?? '').toUpperCase();
  const notice = validationNotice(signal);
  const targets: string[] = Array.isArray(signal.targets) ? signal.targets.map(String) : [];

  return (
    <div className="card">
      <div className="row">
        <strong className="mono">{signal.asset}</strong>
        <strong className={dir === 'LONG' ? 'long' : 'short'}>{dir}</strong>
        <span className="mono">{signal.strategy_id}</span>
        <span className="age age-ok">{String(signal.lifecycle ?? '')}</span>
      </div>

      <div className="row" style={{ marginTop: 10 }}>
        <span>
          <span className="label">entry</span>
          <span className="mono">{signal.entry ?? '—'}</span>
        </span>
        <span>
          <span className="label">stop</span>
          <span className="mono">{signal.stop ?? '—'}</span>
        </span>
        <span>
          <span className="label">target</span>
          <span className="mono">{targets.length ? targets.join(', ') : 'none — trailed'}</span>
        </span>
        {signal.sizing?.size && (
          <span>
            <span className="label">size</span>
            <span className="mono">{signal.sizing.size}</span>
          </span>
        )}
      </div>

      {/* specs/15 failure mode 2: the blank confidence gets replaced by what the trade needs. */}
      <p style={{ margin: '10px 0 0', color: 'var(--muted)' }}>{confidenceCopy(signal)}</p>

      {signal.rationale && <p style={{ margin: '10px 0 0' }}>{signal.rationale}</p>}

      {notice && <div className="unvalidated">{notice}</div>}
    </div>
  );
}

/** specs/15 failure mode 5: say that correlated longs are one position. */
export function ExposureStrip({ signals }: { signals: Signal[] }) {
  const shown = signals.filter(displayable);
  const longs = shown.filter((s) => String(s.direction).toUpperCase() === 'LONG').length;
  const shorts = shown.length - longs;
  const notice = concentrationNotice(longs, shorts);
  return (
    <div className="card">
      <div className="row">
        <span>
          <span className="label">long</span>
          <span className="mono long">{longs}</span>
        </span>
        <span>
          <span className="label">short</span>
          <span className="mono short">{shorts}</span>
        </span>
      </div>
      {notice && <p style={{ margin: '8px 0 0', color: 'var(--act)' }}>{notice}</p>}
    </div>
  );
}

/**
 * Health, grouped by whose problem it is.
 *
 * The engine classifies every failure as an application bug, a configuration mistake, local
 * infrastructure, an external dependency, data quality, or a modelling limit. Showing that
 * classification is the whole point: it is the difference between "read the code" and "restart the
 * tunnel", and a dashboard that renders every failure the same way throws it away.
 */
export function HealthPanel({ payload }: { payload: any }) {
  const components: any[] = payload?.components ?? payload?.data?.components ?? [];
  if (!components.length) {
    return <div className="card empty">The engine did not report any component health.</div>;
  }
  const bad = components.filter((c) => String(c.status).toUpperCase() !== 'HEALTHY');
  return (
    <div className="card">
      <div className="row">
        <strong>{components.length - bad.length} healthy</strong>
        {bad.length > 0 && <strong className="short">{bad.length} not healthy</strong>}
      </div>
      {bad.map((c) => (
        <div key={c.name} style={{ marginTop: 10 }}>
          <div className="row">
            <span className="mono">{c.name}</span>
            <span className="short">{c.status}</span>
            {c.problem_class && <span className="age age-amber">{c.problem_class}</span>}
          </div>
          {c.detail && <p style={{ margin: '4px 0 0', color: 'var(--muted)' }}>{c.detail}</p>}
          {c.remediation && <p style={{ margin: '4px 0 0' }}>{c.remediation}</p>}
        </div>
      ))}
    </div>
  );
}
