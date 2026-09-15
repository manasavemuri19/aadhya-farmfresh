/**
 * Authentication state.
 *
 * The profile lives in memory; only tokens are persisted, and those live in
 * the keychain via `tokenStore`. On cold start we ask the server who we are
 * rather than trusting a cached profile, so a role change or a revoked
 * account takes effect on the next launch.
 */

import { create } from 'zustand';
import { authApi } from '../api/endpoints';
import { tokenStore } from './tokenStore';
import { signOutOfGoogle } from '../lib/googleAuth';
import { ApiError } from '../api/client';
import type { UserProfile } from '../api/types';

interface SessionState {
  user: UserProfile | null;
  // AAD-MOB-002: 'degraded' is a real, distinct outcome of restore() — not a
  // sign-out and not a successful sign-in. It means "we have stored tokens
  // and couldn't confirm them, for a reason that has nothing to do with
  // whether they're still valid". See restore() below for exactly which
  // failures land here versus which still sign out for real.
  status: 'loading' | 'signed_in' | 'signed_out' | 'degraded';
  restore: () => Promise<void>;
  setUser: (user: UserProfile) => void;
  signOut: () => Promise<void>;
}

export const useSession = create<SessionState>((set) => ({
  user: null,
  status: 'loading',

  restore: async () => {
    const token = await tokenStore.getAccessToken();
    if (!token) {
      set({ status: 'signed_out', user: null });
      return;
    }
    try {
      set({ user: await authApi.me(), status: 'signed_in' });
    } catch (error) {
      // AAD-MOB-002: any failure of `me()` used to be treated as "this
      // session is revoked" — a 500, a timeout, a cold start past 40s, or
      // simply no signal at app launch destroyed both the app session and
      // the native Google session. Only a genuine 401/403 — the server
      // itself looked at the token and rejected it — actually means that.
      // Everything else says nothing about whether the session is still
      // good; keep the tokens and let the person retry instead of forcing
      // a fresh sign-in for a problem sign-in wouldn't fix.
      if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
        // Clear the native Google session too, not just our own —
        // otherwise the person lands back on the login screen but the next
        // "Continue with Google" silently re-authenticates as the same
        // (rejected) account instead of letting them pick a different one.
        await Promise.all([tokenStore.clear(), signOutOfGoogle()]);
        set({ status: 'signed_out', user: null });
      } else {
        set({ status: 'degraded', user: null });
      }
    }
  },

  setUser: (user) => set({ user, status: 'signed_in' }),

  signOut: async () => {
    await Promise.all([tokenStore.clear(), signOutOfGoogle()]);
    set({ user: null, status: 'signed_out' });
  },
}));
