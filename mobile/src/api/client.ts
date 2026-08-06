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

const FALLBACK_BASE_URL = 'https://api-base-url-not-set.invalid/v1';
const TIMEOUT_MS = 15_000;

export const API_BASE_URL: string =
  process.env.EXPO_PUBLIC_API_BASE_URL ??
  (Constants.expoConfig?.extra?.apiBaseUrl as string | undefined) ??
  FALLBACK_BASE_URL;

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

/** Shared promise so concurrent 401s trigger exactly one refresh. */
let refreshInFlight: Promise<boolean> | null = null;

async function refreshTokens(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;

  refreshInFlight = (async () => {
    const refreshToken = await tokenStore.getRefreshToken();
    if (!refreshToken) return false;
    try {
      const response = await fetch(`${API_BASE_URL}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      });
      if (!response.ok) {
        await tokenStore.clear();
        return false;
      }
      const tokens = (await response.json()) as TokenPair;
      await tokenStore.save(tokens);
      return true;
    } catch {
      return false;
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
    response = await fetch(`${API_BASE_URL}${path}`, {
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
    if (await refreshTokens()) return send<T>(path, options, true);
    await tokenStore.clear();
  }

  if (response.status === 204) return undefined as T;

  const text = await response.text();
  const payload: unknown = text ? JSON.parse(text) : null;

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
