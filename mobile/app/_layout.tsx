import { useEffect } from 'react';
import { Stack } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import * as SplashScreen from 'expo-splash-screen';
import { useFonts } from 'expo-font';
import { Fraunces_600SemiBold, Fraunces_700Bold } from '@expo-google-fonts/fraunces';
import { DMSans_400Regular, DMSans_500Medium, DMSans_700Bold } from '@expo-google-fonts/dm-sans';
import { DMMono_400Regular, DMMono_500Medium } from '@expo-google-fonts/dm-mono';

import { useSession } from '../src/store/session';
import { ApiError } from '../src/api/client';
import { color } from '../src/theme/tokens';
import { LoginScreen } from '../src/screens/LoginScreen';
import { CompleteProfileScreen } from '../src/screens/CompleteProfileScreen';

void SplashScreen.preventAutoHideAsync();

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
  const { status, user, restore } = useSession();
  const [fontsLoaded, fontError] = useFonts({
    Fraunces_600SemiBold, Fraunces_700Bold,
    DMSans_400Regular, DMSans_500Medium, DMSans_700Bold,
    DMMono_400Regular, DMMono_500Medium,
  });

  useEffect(() => { void restore(); }, [restore]);
  useEffect(() => {
    if ((fontsLoaded || fontError) && status !== 'loading') void SplashScreen.hideAsync();
  }, [fontsLoaded, fontError, status]);

  // Wait for both fonts and the session check before deciding what to show —
  // showing the shop for a flash before redirecting to login would defeat
  // the entire point of gating on it.
  if ((!fontsLoaded && !fontError) || status === 'loading') return null;

  return (
    <QueryClientProvider client={queryClient}>
      <SafeAreaProvider>
        <StatusBar style="dark" />
        {status === 'signed_out' ? (
          <LoginScreen />
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
            <Stack.Screen name="help-support" options={{ title: 'Help & Support' }} />
          </Stack>
        )}
      </SafeAreaProvider>
    </QueryClientProvider>
  );
}
