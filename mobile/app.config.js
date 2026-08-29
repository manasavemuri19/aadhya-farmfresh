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

// IMPORTANT: eas.json's per-profile `env` block is only read by `eas build`.
// `eas update` bundles locally, using whatever is in the calling shell's
// environment at that moment — it does NOT read eas.json's build.*.env at
// all. A person running `eas update` from a plain terminal without that
// variable set would otherwise silently publish a build pointed at nothing.
// So the fallback here is the real, current production API — not a
// deliberately-broken placeholder — because that failure mode has actually
// happened and cost real debugging time. EXPO_PUBLIC_API_BASE_URL still
// overrides this whenever it *is* set (e.g. during a real `eas build`,
// or a local dev server pointed at a different backend).
const API_BASE_URL =
  process.env.EXPO_PUBLIC_API_BASE_URL ??
  'https://aadhya-farmfresh-production.up.railway.app/v1';

// Google OAuth client IDs — same "read at config-eval time, safe default"
// approach as API_BASE_URL above. Empty string (not a fake placeholder) is
// the deliberate default: an empty client ID makes Google's own SDK fail
// immediately and obviously ("invalid_client") rather than pretend to work
// and fail confusingly deeper in the flow.
const GOOGLE_WEB_CLIENT_ID = process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID ?? '';
const GOOGLE_ANDROID_CLIENT_ID = process.env.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID ?? '';

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
      [
        'expo-location',
        {
          locationAlwaysAndWhenInUsePermission:
            'Aadya uses your location to find the right delivery address and estimate arrival time.',
        },
      ],
      // No entry here for @react-native-google-signin/google-signin.
      // Its config plugin exists only to wire up either a Firebase
      // google-services.json (Android) or an iOS URL scheme — neither
      // applies to this Android-only, Firebase-free setup. The native
      // module reads its client ID purely from the JS-level
      // GoogleSignin.configure({ webClientId }) call (see
      // src/lib/googleAuth.ts), confirmed by inspecting the package's own
      // native Android source rather than assumed. Play Services matches
      // the app to its Android OAuth client by package name + signing
      // certificate at request time — nothing else to configure here.
    ],
    updates: {
      url: 'https://u.expo.dev/1b00b0a2-aeb0-4d97-adb0-344bd89331ae',
    },
    runtimeVersion: {
      policy: 'appVersion',
    },
    extra: {
      apiBaseUrl: API_BASE_URL,
      googleWebClientId: GOOGLE_WEB_CLIENT_ID,
      googleAndroidClientId: GOOGLE_ANDROID_CLIENT_ID,
      eas: { projectId: '1b00b0a2-aeb0-4d97-adb0-344bd89331ae' },
    },
  },
};
