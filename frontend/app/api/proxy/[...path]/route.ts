/**
 * Server-side proxy to the Azure VM's API.
 *
 * This file is the whole reason the dashboard can be public while the engine is not. The browser
 * talks only to this route; this route adds the shared token and forwards to the VM. So:
 *
 *  - the token lives in a Vercel environment variable and **never reaches the browser**. It is read
 *    here, on Vercel's server. A `NEXT_PUBLIC_` variable would be compiled into the JavaScript the
 *    browser downloads, which is why neither variable has that prefix;
 *  - the VM needs no CORS configuration, because no browser ever calls it directly;
 *  - the VM's API can stay bound to 127.0.0.1 behind a tunnel, so the Azure firewall keeps every
 *    inbound port closed.
 *
 * **Only GET, and only an allow-listed set of paths.** The engine's API is read-only by
 * construction — there is no endpoint that places an order, because no such code exists anywhere in
 * the project — but this route does not rely on that. It forwards nothing it has not been told to
 * forward, so a future endpoint cannot become publicly reachable by accident.
 */
import { NextRequest, NextResponse } from 'next/server';

export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';

/**
 * Exactly the paths the dashboard reads. An allow-list rather than a pattern: a pattern like
 * `api/**` would quietly expose whatever gets added to the engine later, and the person adding it
 * would have no reason to think about this file.
 */
const ALLOWED = new Set([
  'api/signals/live',
  'api/signals/history',
  'api/health',
  'api/regime',
  'api/warmup',
  'api/events',
]);

/** Paths whose last segment is a variable id, matched by shape. */
const ALLOWED_PREFIXES: RegExp[] = [
  /^api\/signals\/[A-Za-z0-9._-]+$/,
  /^api\/signals\/[A-Za-z0-9._-]+\/audit$/,
  /^api\/assets\/[A-Za-z0-9._-]+$/,
  /^api\/assets\/[A-Za-z0-9._-]+\/rejections$/,
];

function permitted(path: string): boolean {
  return ALLOWED.has(path) || ALLOWED_PREFIXES.some((re) => re.test(path));
}

/** A failure shaped like the engine's own errors, so the UI has one error shape to render. */
function problem(code: string, message: string, likely: string, fix: string, status: number) {
  return NextResponse.json(
    { error: { code, problem_class: 'configuration', message, likely_cause: likely, remediation: fix } },
    { status, headers: { 'Cache-Control': 'no-store' } },
  );
}

export async function GET(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;
  const target = (path ?? []).join('/');

  if (!permitted(target)) {
    return problem(
      'PROXY_PATH_NOT_ALLOWED',
      `this proxy does not forward ${target}`,
      'the dashboard asked for a path that is not on the allow-list in app/api/proxy/[...path]/route.ts',
      'add it to ALLOWED if the dashboard genuinely needs it; the list is explicit so a new engine endpoint cannot become public by accident',
      404,
    );
  }

  const base = process.env.CT_API_BASE;
  const token = process.env.CT_API_TOKEN;

  // Checked separately so the message names which one is missing. "Something is misconfigured" sends
  // someone to read code; "CT_API_TOKEN is not set" sends them to the settings page.
  if (!base) {
    return problem(
      'PROXY_NOT_CONFIGURED',
      'CT_API_BASE is not set, so the dashboard does not know where the engine is',
      'the environment variable is missing on this deployment',
      'set CT_API_BASE in Vercel -> Project -> Settings -> Environment Variables to your tunnel URL, then redeploy',
      503,
    );
  }
  if (!token) {
    return problem(
      'PROXY_NOT_CONFIGURED',
      'CT_API_TOKEN is not set, so requests to the engine would be rejected',
      'the environment variable is missing on this deployment',
      'set CT_API_TOKEN in Vercel to the same value as CT_SERVER__AUTH_TOKEN on the VM, then redeploy',
      503,
    );
  }

  const url = new URL(`${base.replace(/\/+$/, '')}/${target}`);
  // Query parameters are forwarded, but the engine's routes take no parameter that reaches the
  // strategy or risk engines (specs/14 §11), so there is nothing here to influence a suggestion.
  req.nextUrl.searchParams.forEach((v, k) => url.searchParams.set(k, v));

  try {
    const upstream = await fetch(url, {
      headers: { 'x-auth-token': token, accept: 'application/json' },
      // A dashboard that hangs is worse than one that says the engine is unreachable: the reader
      // cannot tell a slow page from a dead engine, and will assume the numbers on screen are current.
      signal: AbortSignal.timeout(8000),
      cache: 'no-store',
    });

    const body = await upstream.text();
    return new NextResponse(body, {
      status: upstream.status,
      headers: {
        'content-type': upstream.headers.get('content-type') ?? 'application/json',
        'Cache-Control': 'no-store',
      },
    });
  } catch (e) {
    const timedOut = e instanceof Error && e.name === 'TimeoutError';
    return NextResponse.json(
      {
        error: {
          code: timedOut ? 'PROXY_UPSTREAM_TIMEOUT' : 'PROXY_UPSTREAM_UNREACHABLE',
          problem_class: 'infrastructure',
          message: timedOut
            ? 'the engine did not answer within 8 seconds'
            : 'the engine could not be reached',
          likely_cause:
            'the Azure VM is off, the tunnel is down, or CT_API_BASE points somewhere else',
          remediation:
            'on the VM: `systemctl status crypto-app crypto-tunnel`. This is an infrastructure problem, not a market one — nothing on this page should be read as current.',
        },
      },
      { status: 503, headers: { 'Cache-Control': 'no-store' } },
    );
  }
}
