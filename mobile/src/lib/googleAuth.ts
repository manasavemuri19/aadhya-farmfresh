/**
 * Google Sign-In, via the Android + Web client ID pair set up in Google
 * Cloud Console (see app.config.js). Uses expo-auth-session's dedicated
 * Google provider rather than the generic OAuth flow — it already knows the
 * right request shape for each client type, so there's no hand-rolled
 * redirect-URI or PKCE logic to get subtly wrong.
 *
 * Only an ID token is requested (not an access token) — the backend only
 * needs to verify who the person is, never to call Google APIs on their
 * behalf, so asking for anything broader would be an unused permission a
 * reviewer (or a suspicious customer) would rightly question.
 */

import { useMemo } from 'react';
import * as Google from 'expo-auth-session/providers/google';
import * as WebBrowser from 'expo-web-browser';
import Constants from 'expo-constants';

WebBrowser.maybeCompleteAuthSession();

const androidClientId = Constants.expoConfig?.extra?.googleAndroidClientId as string | undefined;
const webClientId = Constants.expoConfig?.extra?.googleWebClientId as string | undefined;

export function useGoogleSignIn() {
  const [request, response, promptAsync] = Google.useAuthRequest({
    androidClientId: androidClientId || undefined,
    webClientId: webClientId || undefined,
    responseType: 'id_token',
    scopes: ['openid', 'profile', 'email'],
  });

  const idToken = useMemo(() => {
    if (response?.type !== 'success') return null;
    return response.authentication?.idToken ?? response.params?.id_token ?? null;
  }, [response]);

  const configured = Boolean(androidClientId || webClientId);

  return {
    configured,
    ready: Boolean(request),
    signIn: () => promptAsync(),
    idToken,
    error: response?.type === 'error' ? response.error?.message ?? 'Sign-in was cancelled.' : null,
    cancelled: response?.type === 'cancel' || response?.type === 'dismiss',
  };
}
