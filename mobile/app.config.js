/**
 * Dynamic config, not static app.json.
 *
 * This exists for one reason: `Constants.expoConfig.extra` needs to carry the
 * correct API URL for *this specific build profile* (development / preview /
 * production), and that can only happen if it's computed here, at config-eval
 * time on the EAS build server — where eas.json's per-profile `env` block is
 * guaranteed to already be set in `process.env`.
 *
 * The alternative — relying only on Metro inlining `process.env.EXPO_PUBLIC_*`
 * into the JS bundle — has a fallback path (client.ts) that silently drops to
 * `localhost`, which resolves to the phone itself and fails with a generic,
 * hard-to-diagnose "no connection" on every screen. Setting `extra.apiBaseUrl`
 * here removes that failure mode entirely: both places read the same source,
 * so they cannot diverge, and this one doesn't depend on the bundler doing the
 * substitution correctly.
 */

const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_BASE_URL ??
  // Deliberately not `localhost` — a URL that obviously can't resolve makes a
  // misconfigured build fail loud and fast instead of silently pointing at
  // the phone's own loopback address.
  'https://api-base-url-not-set.invalid/v1';

module.exports = {
  expo: {
    name: 'Aadya',
    slug: 'aadhya-farmfresh',
    scheme: 'aadhya',
    version: '1.0.0',
    orientation: 'portrait',
    userInterfaceStyle: 'light',
    newArchEnabled: false,
    icon: './assets/icon.png',
    splash: {
      image: './assets/splash-logo.png',
      resizeMode: 'contain',
      backgroundColor: '#F4EDE0',
    },
    assetBundlePatterns: ['**/*'],
    ios: {
      supportsTablet: false,
      bundleIdentifier: 'com.aadhya.farmfresh',
    },
    android: {
      package: 'com.aadhya.farmfresh',
      adaptiveIcon: {
        foregroundImage: './assets/adaptive-icon.png',
        backgroundColor: '#F4EDE0',
      },
    },
    web: {
      favicon: './assets/favicon.png',
    },
    plugins: [
      'expo-router',
      'expo-secure-store',
      'expo-font',
      [
        'expo-splash-screen',
        {
          image: './assets/splash-logo.png',
          resizeMode: 'contain',
          backgroundColor: '#F4EDE0',
        },
      ],
    ],
    updates: {
      url: 'https://u.expo.dev/1b00b0a2-aeb0-4d97-adb0-344bd89331ae',
    },
    runtimeVersion: {
      policy: 'appVersion',
    },
    extra: {
      apiBaseUrl: API_BASE_URL,
      eas: { projectId: '1b00b0a2-aeb0-4d97-adb0-344bd89331ae' },
    },
  },
};
