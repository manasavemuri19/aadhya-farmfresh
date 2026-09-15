/**
 * HTTP client.
 *
 * Three things this handles that a bare `fetch` wrapper does not:
 *
 *   1. Token refresh, deduplicated. When several requests 401 at once, exactly
 *      one refresh call goes out and the rest wait on it, rather than each
 *      firing its own and racing to overwrite the stored tokens.
 *   2. Typed errors. The server's `error.code` is preserved so screens can
 *      branch on `out_of_stock` without string-matching a message.
 *   3. Timeouts. A request that hangs on a bad mobile connection fails in
 *      15 seconds instead of leaving a spinner up forever.
 */

import Constants from 'expo-constants';
import type { ApiErrorBody, TokenPair } from './types';
import { tokenStore } from '../store/tokenStore';

// AAD-MOB-003: this used to fall back to the real production API. A
// misconfigured dev build then silently talked to production — real orders,
// real stock decrements, real payment links and real pushes to real
// customers — with nothing on screen to say which environment it was
// hitting. `eas build` never actually needs this fallback: every profile in
// eas.json sets EXPO_PUBLIC_API_BASE_URL explicitly. The only way this
// literal is ever reached is `eas update` run from a shell that hasn't
// exported the variable, or a bare `expo start` on a fresh checkout — and
// for both of those, a development URL is the correct fallback: nothing
// answers on a phone's own LAN address that isn't actually running the dev
// server, so the failure is loud and immediate, never silent and
// destructive. `app.config.js` computes the same value the same way — this
// is the single literal both files defer to, not a second, independently
// maintained one; see `getApiBaseUrl()` below for how the two combine.
const DEV_FALLBACK_BASE_URL = 'http://192.168.1.5:8000/v1';
// Railway's free tier sleeps the backend when idle; the first request after
// that has to wait for a cold boot, which can take 30s+. A short timeout here
// turns that normal wake-up into a scary "took too long" error on the user's
// very first action. 40s covers a cold start with margin.
const TIMEOUT_MS = 40_000;

// Not a plain top-level const — see the equivalent comment in
// src/lib/googleAuth.ts for why. The first option here is safe (inlined at
// bundle time by Metro, not read from a native bridge), but the second
// option, Constants.expoConfig, is read at runtime and shares the exact
// same "read too early after an in-place OTA reload" hazard — and the
// first option is only reliably present when `eas update` was run with
// EXPO_PUBLIC_API_BASE_URL set in the calling shell, which has been missed
// before on this project. A function call re-reads fresh at the moment of
// use rather than caching a possibly-premature read once at import time.
export function getApiBaseUrl(): string {
  return (
    process.env.EXPO_PUBLIC_API_BASE_URL ??
    (Constants.expoConfig?.extra?.apiBaseUrl as string | undefined) ??
    DEV_FALLBACK_BASE_URL
  );
}

// AAD-MOB-003: the rest of this app's "which environment am I talking to"
// question is answered from the single value `app.config.js` computes,
// never re-derived here. `apiEnvironment` is a best-effort label (the real
// EAS build profile or update channel when one was detected at config-eval
// time; 'development' otherwise, never silently 'production') used only to
// decide whether to show the on-screen environment banner — nothing safety-
// critical depends on it. `apiBaseUrlIsFallback` is narrower and is: true
// only when NEITHER an explicit EXPO_PUBLIC_API_BASE_URL NOR a recognised
// EAS build/update context was present at all — a forgotten `.env` on a
// fresh checkout, not a deliberate preview/production build. That's the
// specific case the fix note means by "fail loudly in dev when config is
// missing", surfaced by <EnvironmentBanner /> in `app/_layout.tsx`.
export function getApiEnvironment(): string {
  return (Constants.expoConfig?.extra?.apiEnvironment as string | undefined) ?? 'development';
}

export function isApiBaseUrlUnconfigured(): boolean {
  return Boolean(Constants.expoConfig?.extra?.apiBaseUrlIsFallback);
}

export class ApiError extends Error {
  readonly code: string;
  readonly status: number;
  readonly details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** True when retrying the same request could plausibly succeed. */
  get isRetryable(): boolean {
    return this.status >= 500 || this.code === 'network_error';
  }
}

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE';
  body?: unknown;
  auth?: boolean;
  idempotencyKey?: string;
  signal?: AbortSignal;
}

// AAD-MOB-001: `refreshTokens()` used to return a plain boolean, so a
// rejected refresh token (the session really is over) and a dropped packet
// or a cold-starting backend (the session says nothing about being over)
// were indistinguishable to the caller — both wiped the keychain. A tri-
// state return lets `send()` below clear tokens only when the server
// actually rejected the refresh token; a network failure keeps the session
// and surfaces the original problem instead.
type RefreshResult = 'refreshed' | 'rejected' | 'unavailable';

// Bounded retry with jitter, only for the 'unavailable' case (a dropped
// packet, a timeout, a cold start) — never for 'rejected', where retrying
// would just ask the same already-invalid token again. Full-jitter
// exponential backoff (0..base*2^attempt), capped low: this runs inside a
// user-visible request, so it should smooth over a single bad moment on a
// patchy connection, not turn into its own multi-second stall.
const REFRESH_RETRY_ATTEMPTS = 2;
const REFRESH_RETRY_BASE_MS = 250;

function jitterDelayMs(attempt: number): number {
  return Math.random() * REFRESH_RETRY_BASE_MS * 2 ** attempt;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function attemptRefreshOnce(refreshToken: string): Promise<RefreshResult> {
  try {
    const response = await fetch(`${getApiBaseUrl()}/auth/refresh`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    // 401/403 means the server looked at this specific refresh token and
    // rejected it — expired, revoked, already used. Nothing else does:
    // a 5xx or a malformed response says the *server*, not the token, is the
    // problem, and is worth retrying rather than treating as a dead session.
    if (response.status === 401 || response.status === 403) return 'rejected';
    if (!response.ok) return 'unavailable';
    const tokens = (await response.json()) as TokenPair;
    await tokenStore.save(tokens);
    return 'refreshed';
  } catch {
    return 'unavailable';
  }
}

/** Shared promise so concurrent 401s trigger exactly one refresh. */
let refreshInFlight: Promise<RefreshResult> | null = null;

async function refreshTokens(): Promise<RefreshResult> {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    try {
      const refreshToken = await tokenStore.getRefreshToken();
      // No refresh token stored at all isn't a network condition — there is
      // nothing to retry our way out of.
      if (!refreshToken) return 'rejected';

      let result: RefreshResult = 'unavailable';
      for (let attempt = 0; attempt <= REFRESH_RETRY_ATTEMPTS; attempt++) {
        result = await attemptRefreshOnce(refreshToken);
        if (result !== 'unavailable') break;
        if (attempt < REFRESH_RETRY_ATTEMPTS) await sleep(jitterDelayMs(attempt));
      }

      if (result === 'rejected') await tokenStore.clear();
      return result;
    } finally {
      refreshInFlight = null;
    }
  })();

  return refreshInFlight;
}

async function send<T>(path: string, options: RequestOptions, retrying = false): Promise<T> {
  const { method = 'GET', body, auth = false, idempotencyKey, signal } = options;

  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (idempotencyKey) headers['Idempotency-Key'] = idempotencyKey;
  if (auth) {
    const token = await tokenStore.getAccessToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), TIMEOUT_MS);
  if (signal) signal.addEventListener('abort', () => controller.abort());

  let response: Response;
  try {
    response = await fetch(`${getApiBaseUrl()}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (error) {
    const aborted = error instanceof Error && error.name === 'AbortError';
    throw new ApiError(
      0,
      aborted ? 'timeout' : 'network_error',
      aborted
        ? 'That took too long. Check your connection and try again.'
        : 'No connection. Check your network and try again.',
    );
  } finally {
    clearTimeout(timeout);
  }

  // A 401 on an authenticated call means the access token aged out. Refresh
  // once, then replay. `retrying` stops an infinite loop if refresh also 401s.
  if (response.status === 401 && auth && !retrying) {
    const result = await refreshTokens();
    if (result === 'refreshed') return send<T>(path, options, true);
    if (result === 'unavailable') {
      // AAD-MOB-001: the refresh token might still be perfectly good — the
      // network just didn't cooperate. Tokens are NOT cleared here (see
      // refreshTokens()); surface the real problem and let the caller retry
      // the whole request later, rather than reporting the original
      // resource's 401 (misleading — this request was never actually denied
      // access, its token just couldn't be renewed in time).
      throw new ApiError(
        0,
        'network_error',
        'No connection. Check your network and try again.',
      );
    }
    // 'rejected' — tokens already cleared inside refreshTokens(). Fall
    // through to the original 401 response below, same as before this fix.
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  let payload: unknown = null;
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      // AAD-MOB-004: Railway and most proxies return an HTML error page for
      // 502/503/504, not JSON. An unguarded JSON.parse throws a SyntaxError
      // here, which isn't an ApiError, so every caller's `instanceof
      // ApiError` branch is bypassed and the app shows an unhandled crash
      // instead of "try again". Treating an unparsable body the same as an
      // empty one lets the existing logic below do the right thing either
      // way: the `!response.ok` branch synthesizes a generic ApiError from
      // a null payload (there's no `error.code` to read, so it uses its own
      // fallback message), and a 2xx with an unparsable body degrades to
      // `null` rather than crashing.
      payload = null;
    }
  }

  if (!response.ok) {
    const err = (payload as ApiErrorBody | null)?.error;
    throw new ApiError(
      response.status,
      err?.code ?? 'unknown_error',
      err?.message ?? 'Something went wrong. Try again.',
      err?.details ?? {},
    );
  }

  return payload as T;
}

export const api = {
  get: <T>(path: string, auth = false) => send<T>(path, { method: 'GET', auth }),
  post: <T>(path: string, body?: unknown, opts: Omit<RequestOptions, 'method' | 'body'> = {}) =>
    send<T>(path, { ...opts, method: 'POST', body }),
  patch: <T>(path: string, body?: unknown, auth = true) =>
    send<T>(path, { method: 'PATCH', body, auth }),
  put: <T>(path: string, body?: unknown, auth = true) =>
    send<T>(path, { method: 'PUT', body, auth }),
};
