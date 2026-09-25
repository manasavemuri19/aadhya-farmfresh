import { useEffect } from 'react';
import { AppState, type AppStateStatus } from 'react-native';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { focusManager, QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as SplashScreen from 'expo-splash-screen';
import { useFonts } from 'expo-font';
import { Fraunces_600SemiBold, Fraunces_700Bold } from '@expo-google-fonts/fraunces';
import { DMSans_400Regular, DMSans_500Medium, DMSans_700Bold } from '@expo-google-fonts/dm-sans';
import { DMMono_400Regular, DMMono_500Medium } from '@expo-google-fonts/dm-mono';

import { useSession } from '../src/store/session';
import { ApiError, getApiBaseUrl, isApiBaseUrlUnconfigured } from '../src/api/client';
import { color } from '../src/theme/tokens';
import { LoginScreen } from '../src/screens/LoginScreen';
import { CompleteProfileScreen } from '../src/screens/CompleteProfileScreen';
import { EnvironmentBanner } from '../src/components/EnvironmentBanner';
import { ErrorState } from '../src/components/Feedback';
import { registerForPushNotifications } from '../src/lib/pushNotifications';

// AAD-MOB-013: catches a render error anywhere in this app — including one
// thrown by RootLayout itself (isProfileComplete below, or the font/session
// gating render) — so the failure mode is a retry screen, not a permanent
// blank one. Per-screen ErrorBoundary exports on order/[id] and payment
// narrow the blast radius further for the two riskiest screens; this one is
// the backstop for everything else.
export { AppErrorFallback as ErrorBoundary } from '../src/components/ErrorBoundary';

void SplashScreen.preventAutoHideAsync();

// AAD-MOB-011: React Query only pauses refetchInterval on backgrounding when
// focusManager is told what "focused" means for this platform — on the web
// it infers this from document.visibilitychange automatically, but React
// Native has no such default, so without this every poll (order tracking,
// payment status, the delivery agent's ongoing/requests lists) keeps firing
// at full frequency indefinitely while the app sits backgrounded. One
// listener here fixes every refetchInterval call site in the app at once —
// none of them need to know this exists.
function onAppStateChange(status: AppStateStatus): void {
  focusManager.setFocused(status === 'active');
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      retry: (failureCount, error) =>
        error instanceof ApiError ? error.isRetryable && failureCount < 2 : failureCount < 2,
    },
    mutations: { retry: false },
  },
});

// Mandatory at registration: name, phone, and a saved address. Checked here
// rather than only at signup, so an account that somehow ended up
// incomplete (an interrupted signup, a manual DB fix) is caught on every
// launch, not just the first one.
function isProfileComplete(user: { name: string; phone: string | null; addresses: unknown[] } | null): boolean {
  if (!user) return false;
  return Boolean(user.name.trim()) && Boolean(user.phone?.trim()) && user.addresses.length > 0;
}

export default function RootLayout() {
  const { status, user, degradedError, restore } = useSession();
  const [fontsLoaded, fontError] = useFonts({
    Fraunces_600SemiBold, Fraunces_700Bold,
    DMSans_400Regular, DMSans_500Medium, DMSans_700Bold,
    DMMono_400Regular, DMMono_500Medium,
  });

  useEffect(() => {
    const subscription = AppState.addEventListener('change', onAppStateChange);
    return () => subscription.remove();
  }, []);

  useEffect(() => { void restore(); }, [restore]);
  // AAD-MOB-003: "fail loudly in dev when config is missing" — a forgotten
  // .env on a fresh checkout is otherwise silent (the fallback URL just
  // doesn't answer, which looks like an ordinary network error). This is
  // the one unmissable signal in the one place a developer will actually
  // look. <EnvironmentBanner /> covers the always-visible on-screen half.
  useEffect(() => {
    if (__DEV__ && isApiBaseUrlUnconfigured()) {
      console.error(
        `[Aadya] No EXPO_PUBLIC_API_BASE_URL configured and no EAS build/update ` +
          `context detected — falling back to ${getApiBaseUrl()}. Set ` +
          `EXPO_PUBLIC_API_BASE_URL (see mobile/.env.example) before testing anything ` +
          `that talks to the backend.`,
      );
    }
  }, []);
  useEffect(() => {
    if ((fontsLoaded || fontError) && status !== 'loading') void SplashScreen.hideAsync();
  }, [fontsLoaded, fontError, status]);
  // Register for push once someone is actually signed in with a complete
  // profile — the same gate the screen below uses to decide what to render.
  // Registering earlier would have no account on the backend to attach the
  // token to; registering on every render would spam the endpoint, which is
  // why registerForPushNotifications guards its own re-entrancy too.
  useEffect(() => {
    if (status === 'signed_in' && isProfileComplete(user)) {
      void registerForPushNotifications();
    }
  }, [status, user]);

  // Wait for both fonts and the session check before deciding what to show —
  // showing the shop for a flash before redirecting to login would defeat
  // the entire point of gating on it.
  if ((!fontsLoaded && !fontError) || status === 'loading') return null;

  return (
    <QueryClientProvider client={queryClient}>
      <SafeAreaProvider>
        <StatusBar style="dark" />
        <EnvironmentBanner />
        {status === 'signed_out' ? (
          <LoginScreen />
        ) : status === 'degraded' ? (
          // AAD-MOB-002: the profile fetch failed for a reason that says
          // nothing about whether the session is still valid — a dropped
          // packet, a timeout, a cold-starting backend, a 500. Tokens were
          // kept (see session.ts's restore()); this is a retry, not a
          // re-login. AAD-MOB-026: message now comes from the same shared
          // two-bucket split every other failed screen uses, since restore()
          // can land here for either reason (see its own comment).
          <ErrorState error={degradedError} onRetry={() => void restore()} />
        ) : !isProfileComplete(user) ? (
          <CompleteProfileScreen />
        ) : (
          <Stack
            screenOptions={{
              headerShadowVisible: false,
              headerStyle: { backgroundColor: color.surface },
              headerTintColor: color.ink,
              headerTitleStyle: { fontFamily: 'Fraunces_600SemiBold', fontSize: 18 },
              contentStyle: { backgroundColor: color.surface },
            }}
          >
            <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
            <Stack.Screen name="cart" options={{ title: 'Your cart' }} />
            <Stack.Screen name="checkout" options={{ title: 'Checkout' }} />
            <Stack.Screen name="payment" options={{ title: 'Payment', headerBackVisible: false }} />
            <Stack.Screen name="payment-callback" options={{ title: 'Payment', headerBackVisible: false }} />
            <Stack.Screen name="orders" options={{ title: 'My orders' }} />
            <Stack.Screen name="order/[id]" options={{ title: 'Order' }} />
            <Stack.Screen name="order-edit-address" options={{ title: 'Delivery address' }} />
            <Stack.Screen name="edit-details" options={{ title: 'Edit details' }} />
            <Stack.Screen name="addresses" options={{ title: 'Saved addresses' }} />
            <Stack.Screen name="address-edit" options={{ title: 'Address' }} />
            <Stack.Screen name="help-support" options={{ title: 'Help & Support' }} />
          </Stack>
        )}
      </SafeAreaProvider>
    </QueryClientProvider>
  );
}
