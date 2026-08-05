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
  } catch {
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
