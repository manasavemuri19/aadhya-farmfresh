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
// all. A person running `eas update` (or a bare `expo start`) without that
// variable set would otherwise silently point the bundle at whatever this
// fallback is.
//
// AAD-SEC/AAD-MOB-003 (2026-09-15): that fallback used to be the real
// production API, on the theory that a deliberately-broken placeholder
// "cost real debugging time once already". The actual cost of the opposite
// mistake is worse: a misconfigured dev build silently creating real
// orders, decrementing real stock, and sending real payment links and real
// pushes to real customers, with nothing on screen to say so. `eas build`
// never needs this fallback at all — every profile below sets
// EXPO_PUBLIC_API_BASE_URL explicitly — so the only paths that ever reach
// it are `eas update` run without the variable exported, or local
// development. A development URL is the right fallback for both: nothing
// answers on a phone's own LAN address unless the dev server is actually
// running there, so the failure is loud and immediate instead of silent and
// destructive. `src/api/client.ts`'s `getApiBaseUrl()` defers to this same
// computed `extra.apiBaseUrl` value rather than keeping its own separate
// fallback literal — one source, not two that can drift.
const DEV_FALLBACK_API_BASE_URL = 'http://192.168.1.5:8000/v1';

const API_BASE_URL = process.env.EXPO_PUBLIC_API_BASE_URL ?? DEV_FALLBACK_API_BASE_URL;

// Best-effort label for the on-screen environment banner
// (`src/components/EnvironmentBanner.tsx`) — nothing safety-critical reads
// this. EAS sets one of these two env vars at config-eval time, matching
// the profile/channel name: EAS_BUILD_PROFILE during `eas build`,
// EAS_UPDATE_CHANNEL during `eas update --channel <name>`. Neither present
// means this is a bare `expo start` with no EAS context at all — always
// 'development' by definition, never silently 'production'.
const API_ENVIRONMENT =
  process.env.EAS_BUILD_PROFILE ?? process.env.EAS_UPDATE_CHANNEL ?? 'development';

// True only when NEITHER an explicit EXPO_PUBLIC_API_BASE_URL NOR a
// recognised EAS build/update context was present — a forgotten `.env` on a
// fresh checkout, not a deliberate preview/production build. This is the
// specific case the fix note means by "fail loudly in dev when config is
// missing": `EnvironmentBanner` shows an unmissable warning for this case
// specifically, not just the ordinary "development" badge.
const API_BASE_URL_IS_FALLBACK =
  !process.env.EXPO_PUBLIC_API_BASE_URL &&
  !process.env.EAS_BUILD_PROFILE &&
  !process.env.EAS_UPDATE_CHANNEL;

// Google OAuth client IDs — same "read at config-eval time, safe default"
// approach as API_BASE_URL above.
//
// These USED to default to '' when the env var was unset, on the theory
// that an empty client ID makes Google's SDK fail immediately and
// obviously. In practice that default is exactly what silently ships on
// every `eas update` run from a shell that hasn't exported these vars
// (see the note above: `eas update` does not read eas.json's build.*.env)
// — which is indistinguishable from "sign-in is broken" to anyone using
// the app. Falling back to the real production client IDs, sourced
// straight from eas.json's production profile, removes that failure mode
// the same way the API_BASE_URL fallback does above.
const easBuildEnv = (() => {
  try {
    return require('./eas.json')?.build?.production?.env ?? {};
  } catch {
    return {};
  }
})();

const GOOGLE_WEB_CLIENT_ID =
  process.env.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID ??
  easBuildEnv.EXPO_PUBLIC_GOOGLE_WEB_CLIENT_ID ??
  '';
const GOOGLE_ANDROID_CLIENT_ID =
  process.env.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID ??
  easBuildEnv.EXPO_PUBLIC_GOOGLE_ANDROID_CLIENT_ID ??
  '';

// Google Maps Platform key (Maps SDK for Android / Directions / Geocoding),
// restricted in Google Cloud Console to this app's package name + signing
// keystore SHA-1. This key is only ever read at native-build time (baked
// into the Android manifest by Expo's prebuild step), unlike the others
// which the JS bundle also reads at runtime.
//
// SECURITY (2026-09-12): this used to fall back to a real, hardcoded key —
// the same "keep a working default so `eas update` never silently ships a
// broken build" reasoning as the two client IDs above. That key leaked via
// this public repo (it was readable in plain text in every commit) and has
// been revoked; it must never be replaced with another literal value here.
// The only supported sources now are a real `EAS secret`/`eas env` variable
// or a local `.env` — see mobile/.env.example. An empty key means Maps
// tiles won't load until one of those is configured; that is the correct,
// visible failure mode for a credential that must never be committed again.
const GOOGLE_MAPS_API_KEY =
  process.env.GOOGLE_MAPS_API_KEY ??
  easBuildEnv.GOOGLE_MAPS_API_KEY ??
  '';

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
      // Added for FCM push notifications (project "aadya-dairy"). Safe to
      // commit — contains only public app/project identifiers, no secrets.
      // Wired to expo-notifications (see the plugins array below) and the
      // token-registration code in src/lib/pushNotifications.ts.
      googleServicesFile: './google-services.json',
      config: {
        googleMaps: {
          apiKey: GOOGLE_MAPS_API_KEY,
        },
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
      'expo-notifications',
      // No entry here for @react-native-google-signin/google-signin, even
      // though a Firebase google-services.json now exists above (added for
      // FCM push notifications, not for sign-in). This package's config
      // plugin exists only to wire up a Firebase google-services.json or an
      // iOS URL scheme for ITS OWN purposes — Google Sign-In here doesn't
      // use either. The native module reads its client ID purely from the
      // JS-level GoogleSignin.configure({ webClientId }) call (see
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
      apiEnvironment: API_ENVIRONMENT,
      apiBaseUrlIsFallback: API_BASE_URL_IS_FALLBACK,
      googleWebClientId: GOOGLE_WEB_CLIENT_ID,
      googleAndroidClientId: GOOGLE_ANDROID_CLIENT_ID,
      eas: { projectId: '1b00b0a2-aeb0-4d97-adb0-344bd89331ae' },
    },
  },
};
