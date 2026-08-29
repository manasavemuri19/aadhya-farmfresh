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

const webClientId = Constants.expoConfig?.extra?.googleWebClientId as string | undefined;

let configured = false;
function ensureConfigured() {
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
  if (!webClientId) {
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
  return Boolean(webClientId);
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
