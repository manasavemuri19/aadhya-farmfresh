/**
 * Token persistence.
 *
 * Tokens go in the OS keychain (expo-secure-store), never AsyncStorage —
 * AsyncStorage is plain text on disk and readable on a rooted device. Reads
 * are memoised because the client checks the access token on every request
 * and a keychain read on each one is measurably slow.
 */

import * as SecureStore from 'expo-secure-store';
import type { TokenPair } from '../api/types';

const ACCESS_KEY = 'aadhya.access_token';
const REFRESH_KEY = 'aadhya.refresh_token';

let cachedAccess: string | null | undefined;
let cachedRefresh: string | null | undefined;

async function read(key: string): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(key);
  } catch (err) {
    // AAD-MOB-008: `getItemAsync` already resolves to `null` for the
    // ordinary "nothing stored under this key" case — it doesn't throw for
    // that. Anything landing in this catch is SecureStore itself failing
    // (a locked/unavailable keystore, a corrupted entry, an OS-level
    // permission problem), which used to be swallowed and returned as the
    // exact same `null` a logged-out user produces. That made a genuine
    // keychain failure indistinguishable from "never signed in" all the way
    // up through `getAccessToken`/`getRefreshToken` — the app would just
    // sign someone out with nothing in the logs to explain why. Still
    // returns `null` (there's no token to hand back either way, and callers
    // shouldn't have to handle a third state), but now leaves a trail.
    console.error(`[tokenStore] SecureStore read failed for "${key}":`, err);
    return null;
  }
}

export const tokenStore = {
  async getAccessToken(): Promise<string | null> {
    if (cachedAccess === undefined) cachedAccess = await read(ACCESS_KEY);
    return cachedAccess;
  },

  async getRefreshToken(): Promise<string | null> {
    if (cachedRefresh === undefined) cachedRefresh = await read(REFRESH_KEY);
    return cachedRefresh;
  },

  async save(tokens: TokenPair): Promise<void> {
    cachedAccess = tokens.access_token;
    cachedRefresh = tokens.refresh_token;
    await Promise.all([
      SecureStore.setItemAsync(ACCESS_KEY, tokens.access_token),
      SecureStore.setItemAsync(REFRESH_KEY, tokens.refresh_token),
    ]);
  },

  async clear(): Promise<void> {
    cachedAccess = null;
    cachedRefresh = null;
    await Promise.all([
      SecureStore.deleteItemAsync(ACCESS_KEY).catch(() => undefined),
      SecureStore.deleteItemAsync(REFRESH_KEY).catch(() => undefined),
    ]);
  },
};
