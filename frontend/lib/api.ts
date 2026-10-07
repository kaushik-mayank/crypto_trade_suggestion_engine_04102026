/**
 * Fetching, from the browser, through our own proxy route.
 *
 * Every call goes to `/api/proxy/...` on this same Vercel deployment — never to the engine directly.
 * That is what keeps the shared token server-side and means the Azure firewall never has to open a
 * port.
 */

export type Envelope = {
  data_as_of_ms?: number | null;
  served_at_ms?: number | null;
  age_seconds?: number | null;
  system_status?: string;
  detail?: string;
};

export type ApiResult<T = any> = {
  ok: boolean;
  status: number;
  payload: T | null;
  /** Set when the request itself failed, as distinct from the engine answering with a problem. */
  transportError?: string;
};

async function get<T>(path: string, search?: Record<string, string>): Promise<ApiResult<T>> {
  const qs = search ? `?${new URLSearchParams(search).toString()}` : '';
  try {
    const res = await fetch(`/api/proxy/${path}${qs}`, { cache: 'no-store' });
    const payload = (await res.json().catch(() => null)) as T | null;
    return { ok: res.ok, status: res.status, payload };
  } catch (e) {
    // The page is open and the network is gone. Reported as its own state rather than as an empty
    // result, for the same reason the engine distinguishes DOWN from empty.
    return {
      ok: false,
      status: 0,
      payload: null,
      transportError: e instanceof Error ? e.message : 'the request could not be sent',
    };
  }
}

export const api = {
  liveSignals: () => get('api/signals/live'),
  health: () => get('api/health'),
  regime: () => get('api/regime'),
  warmup: () => get('api/warmup'),
  rejections: (asset: string) => get(`api/assets/${encodeURIComponent(asset)}/rejections`),
};

/**
 * How often the page re-reads the engine.
 *
 * 15 seconds, matching the engine's own fastest poll. Faster would show the same numbers again and
 * spend the venue's rate budget through the proxy for nothing; much slower and the age counter on
 * screen starts being the only thing that moves, which is how a reader stops noticing it.
 */
export const REFRESH_MS = 15_000;
