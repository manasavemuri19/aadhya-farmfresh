/**
 * AAD-MOB-013: the shared fallback for every `ErrorBoundary` export in this
 * app — one at the root (`app/_layout.tsx`), catching anything that would
 * otherwise white-screen the whole app at launch, and one each on
 * `order/[id]` and `payment` (the screens most likely to throw: status-keyed
 * lookups, an untyped `checkout_payload` field), so a bad render there
 * replaces only that screen's content rather than the whole navigation
 * stack.
 *
 * Reuses `ErrorState`'s existing visual language rather than inventing a
 * second "something went wrong" look. `retry` is Expo Router's own
 * `Try.retry` (a `() => Promise<void>` that clears the boundary's caught
 * error and re-renders the route it wraps) — not a full app restart.
 */

import { useEffect } from 'react';

import { ErrorState } from './Feedback';

// No crash-reporting backend (Sentry, Crashlytics) is wired into this app
// yet — that's a real gap (see the finding's own "add crash reporting"
// suggestion), but adding one needs an account and a DSN, which is a
// decision for you to make, not something to guess at in a code fix. This
// at least makes a caught render error visible in whatever log you already
// have (Metro/adb logcat in dev, whatever the production log sink is),
// instead of vanishing the moment the boundary catches it.
function reportCaughtError(error: Error): void {
  // eslint-disable-next-line no-console
  console.error('[ErrorBoundary] caught a render error:', error);
}

export function AppErrorFallback({ error, retry }: { error: Error; retry: () => Promise<void> }) {
  useEffect(() => {
    reportCaughtError(error);
  }, [error]);

  // AAD-MOB-026: a caught render crash is never an ApiError, so
  // describeError(error) always lands on its "something went wrong" bucket
  // here — correct by construction, no special-casing needed.
  return <ErrorState error={error} onRetry={() => void retry()} />;
}
