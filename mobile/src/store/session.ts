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
import type { UserProfile } from '../api/types';

interface SessionState {
  user: UserProfile | null;
  status: 'loading' | 'signed_in' | 'signed_out';
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
    } catch {
      // The stored app token is invalid or was revoked server-side. Clear
      // the native Google session too, not just our own — otherwise the
      // person lands back on the login screen but the next "Continue with
      // Google" silently re-authenticates as the same (rejected) account
      // instead of letting them pick a different one.
      await Promise.all([tokenStore.clear(), signOutOfGoogle()]);
      set({ status: 'signed_out', user: null });
    }
  },

  setUser: (user) => set({ user, status: 'signed_in' }),

  signOut: async () => {
    await Promise.all([tokenStore.clear(), signOutOfGoogle()]);
    set({ user: null, status: 'signed_out' });
  },
}));
