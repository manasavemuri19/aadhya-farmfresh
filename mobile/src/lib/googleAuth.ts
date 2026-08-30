/**
 * Google Sign-In, via the native Play Services SDK — not a browser redirect.
 *
 * The first version of this used expo-auth-session's generic OAuth flow with
 * a custom `aadhya://` redirect scheme. That does not work with an "Android"
 * type OAuth client in Google Cloud Console: Android clients are designed to
 * be used with Google's native sign-in SDK, which authenticates via Play
 * Services using the app's package name and signing certificate directly —
 * there is no redirect URI in that flow at all. A browser-redirect attempt
 * against an Android client fails with "Custom URI scheme is not enabled for
 * your Android client", which is Google's deliberate security boundary, not
 * a setting to switch on.
 *
 * Only `webClientId` is passed to `configure()` — this is correct, not a
 * simplification: the Android client is never referenced by ID in code at
 * all. Play Services matches the app to its Android OAuth client purely by
 * package name + signing certificate at request time, and the ID token that
 * comes back is issued for the *web* client, which is exactly what the
 * backend verifies against.
 *
 * Pinned to v10.1.2 deliberately: newer major versions changed `signIn()` to
 * return a wrapped `{ type, data }` result. This version's `signIn()`
 * resolves the plain user object directly and signals cancellation by
 * throwing an error coded `SIGN_IN_CANCELLED`, not by a `response.type`
 * check — mixing the two API shapes is an easy, silent mistake.
 */

import Constants from 'expo-constants';
import { GoogleSignin, statusCodes } from '@react-native-google-signin/google-signin';

/**
 * Read fresh every time, not cached in a module-level constant. A
 * module-top-level read only runs once, the moment this file is first
 * imported — normally fine, but an OTA update reloads the JS bundle in
 * place without a full app restart, and that reload can re-run this line
 * before the native Constants bridge has finished re-initialising. The
 * value baked into the app never changes; reading it too early can still
 * come back empty. A function call re-reads it at the actual moment of use,
 * by which point the app has been running and interactive for a while —
 * there's no realistic "too early" left.
 */
function getWebClientId(): string | undefined {
  return Constants.expoConfig?.extra?.googleWebClientId as string | undefined;
}

let configured = false;
function ensureConfigured() {
  const webClientId = getWebClientId();
  if (configured || !webClientId) return;
  GoogleSignin.configure({ webClientId });
  configured = true;
}

export interface GoogleSignInResult {
  idToken: string;
}

/** Returns null on cancellation — that's not an error, just nothing to do. */
export async function signInWithGoogle(): Promise<GoogleSignInResult | null> {
  ensureConfigured();
  if (!getWebClientId()) {
    throw new Error("Sign-in isn't configured yet on this build.");
  }

  try {
    await GoogleSignin.hasPlayServices({ showPlayServicesUpdateDialog: true });
    const user = await GoogleSignin.signIn();
    if (!user.idToken) {
      throw new Error('Google did not return a usable sign-in token. Try again.');
    }
    return { idToken: user.idToken };
  } catch (err) {
    if (isCancelled(err)) return null;
    throw new Error(describeGoogleSignInError(err));
  }
}

export function isGoogleConfigured(): boolean {
  return Boolean(getWebClientId());
}

/**
 * Clears the *native* Google session, not just this app's own tokens.
 *
 * `GoogleSignin.signIn()` is designed to be silent on a return visit — Play
 * Services caches the last-used account and hands it straight back without
 * showing the chooser, which is normally the desired, low-friction behaviour.
 * But it means that if our own sign-out only clears the app's JWTs (see
 * session.ts) and never tells the native SDK to forget the account, every
 * subsequent "Continue with Google" — and "Register with Google", which is
 * the exact same call — silently re-authenticates as whoever was last
 * signed in. There is no way to switch or add an account, and no way to
 * test the "new user" registration path a second time on the same device.
 *
 * `GoogleSignin.signOut()` only clears that local cache; it does not revoke
 * the app's access grant (that's `revokeAccess()`, and isn't what's needed
 * here — the person should still count as a returning user of the *app* if
 * they pick the same account again, just via the chooser this time).
 *
 * Deliberately swallows errors: this runs as part of app-level sign-out,
 * which must always succeed. Calling it while there is no active native
 * session (e.g. never configured yet, or already signed out) throws, and
 * that's not a real failure worth surfacing.
 */
export async function signOutOfGoogle(): Promise<void> {
  try {
    ensureConfigured();
    await GoogleSignin.signOut();
  } catch {
    // No active Google session to clear — nothing to do.
  }
}

function isCancelled(err: unknown): boolean {
  return Boolean(err && typeof err === 'object' && 'code' in err && err.code === statusCodes.SIGN_IN_CANCELLED);
}

function describeGoogleSignInError(err: unknown): string {
  const code = err && typeof err === 'object' && 'code' in err ? (err as { code: string }).code : null;
  switch (code) {
    case statusCodes.PLAY_SERVICES_NOT_AVAILABLE:
      return 'Google Play Services is required and could not be reached.';
    case statusCodes.IN_PROGRESS:
      return 'Still signing in — one moment.';
    default:
      return err instanceof Error ? err.message : 'Could not sign in with Google. Try again.';
  }
}
