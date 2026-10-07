'use client';

/**
 * The dashboard.
 *
 * It reads three endpoints through the proxy and renders exactly what they say. Every judgement
 * about whether something is fresh, degraded or unvalidated is made by the engine and travels in
 * the payload; this page's only job is to not discard that and to not present any of it as more
 * certain than it is.
 */
import { useCallback, useEffect, useState } from 'react';

import { ActionBar, Down, Empty, ExposureStrip, Freshness, HealthPanel, SignalCard, Warming } from '@/components/Panels';
import { REFRESH_MS, api, type ApiResult } from '@/lib/api';
import { panelState } from '@/lib/honesty';

type State = {
  signals: ApiResult | null;
  health: ApiResult | null;
  /** When the browser last completed a read. Distinct from the data's own age. */
  fetchedAt: number | null;
  loading: boolean;
};

export default function Page() {
  const [state, setState] = useState<State>({ signals: null, health: null, fetchedAt: null, loading: true });

  const refresh = useCallback(async () => {
    const [signals, health] = await Promise.all([api.liveSignals(), api.health()]);
    setState({ signals, health, fetchedAt: Date.now(), loading: false });
  }, []);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), REFRESH_MS);
    return () => clearInterval(t);
  }, [refresh]);

  const sig = state.signals;
  const panel = sig?.transportError
    ? ({ kind: 'down', message: sig.transportError, likelyCause: 'the browser could not reach this page’s own server' } as const)
    : panelState(sig?.payload);

  const envelope = (sig?.payload as any)?.envelope ?? {};
  const data: any[] = (sig?.payload as any)?.data ?? [];

  return (
    <main className="wrap">
      <h1>Trade suggestions</h1>

      {/* Stated on every load, not buried in an about page. Someone reading this under time
          pressure should not have to remember what the system is allowed to do. */}
      <div className="banner never-trades">
        Read-only. This system never places, cancels or transfers anything — it suggests, and a
        person decides. Nothing here has been validated against history yet.
      </div>

      {state.loading && <div className="card empty">Reading the engine…</div>}

      {!state.loading && panel.kind === 'down' && <Down state={panel} />}
      {!state.loading && panel.kind === 'warming' && <Warming detail={panel.detail} />}

      {!state.loading && panel.kind === 'ready' && (
        <>
          <ActionBar signals={data} />

          <h2>Freshness</h2>
          <div className="card">
            <div className="row">
              <Freshness ageSeconds={envelope.age_seconds} label="market data" />
              {state.fetchedAt && (
                <span className="age age-ok">
                  page read {Math.round((Date.now() - state.fetchedAt) / 1000)}s ago
                </span>
              )}
            </div>
            {/* The two ages are shown separately on purpose: a page that refreshed a second ago can
                still be showing market data from an hour ago, and one number cannot say that. */}
            <p style={{ margin: '8px 0 0', color: 'var(--muted)' }}>
              The first is how old the market data is. The second is how long since this page
              re-read it. A fresh page read does not make old data current.
            </p>
          </div>

          <h2>Exposure</h2>
          <ExposureStrip signals={data} />

          <h2>Suggestions</h2>
          {data.length === 0 ? (
            <Empty
              reason={
                envelope.detail ??
                'No strategy met all of its conditions on the latest closed bar.'
              }
            />
          ) : (
            data.map((s) => <SignalCard key={s.signal_id} signal={s} />)
          )}
        </>
      )}

      <h2>System health</h2>
      {state.health?.transportError || (state.health?.payload as any)?.error ? (
        <Down
          state={{
            kind: 'down',
            message:
              state.health?.transportError ??
              (state.health?.payload as any)?.error?.message ??
              'health could not be read',
          }}
        />
      ) : (
        <HealthPanel payload={(state.health?.payload as any)?.data ?? state.health?.payload} />
      )}

      <footer>
        Refreshes every {REFRESH_MS / 1000}s. Suggestions are produced by twelve strategies across
        three assets; none has passed validation, so each is sized at a reduced multiple and marked
        accordingly.
      </footer>
    </main>
  );
}
