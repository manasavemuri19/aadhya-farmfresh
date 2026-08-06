

const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_BASE_URL ??
  // Deliberately not `localhost` — a URL that obviously can't resolve makes a
  // misconfigured build fail loud and fast instead of silently pointing at
  // the phone's own loopback address.
  'https://api-base-url-not-set.invalid/v1';

module.exports = {
  expo: {
    name: 'Aadhya Pickles & Dairy',
    slug: 'aadhya-farmfresh',
    scheme: 'aadhya',
    version: '1.0.0',
    orientation: 'portrait',
    userInterfaceStyle: 'light',
    newArchEnabled: false,
    splash: {
      resizeMode: 'contain',
      backgroundColor: '#16261D',
    },
    assetBundlePatterns: ['**/*'],
    ios: {
      supportsTablet: false,
      bundleIdentifier: 'com.aadhya.farmfresh',
    },
    android: {
      package: 'com.aadhya.farmfresh',
      adaptiveIcon: { backgroundColor: '#16261D' },
    },
    plugins: ['expo-router', 'expo-secure-store', 'expo-font'],
    extra: {
      apiBaseUrl: API_BASE_URL,
      eas: { projectId: process.env.EAS_PROJECT_ID ?? '' },
    },
  },
};