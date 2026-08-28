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

export default function RootLayout() {
  const restore = useSession((s) => s.restore);
  const [fontsLoaded, fontError] = useFonts({
    Fraunces_600SemiBold, Fraunces_700Bold,
    DMSans_400Regular, DMSans_500Medium, DMSans_700Bold,
    DMMono_400Regular, DMMono_500Medium,
  });

  useEffect(() => { void restore(); }, [restore]);
  useEffect(() => {
    if (fontsLoaded || fontError) void SplashScreen.hideAsync();
  }, [fontsLoaded, fontError]);

  if (!fontsLoaded && !fontError) return null;

  return (
    <QueryClientProvider client={queryClient}>
      <SafeAreaProvider>
        <StatusBar style="dark" />
        <Stack
          screenOptions={{
            headerShadowVisible: false,
            headerStyle: { backgroundColor: color.surface },
            headerTintColor: color.ink,
            headerTitleStyle: { fontFamily: 'Fraunces_600SemiBold', fontSize: 18 },
            contentStyle: { backgroundColor: color.surface },
          }}
        >
          <Stack.Screen name="index" options={{ headerShown: false }} />
          <Stack.Screen name="cart" options={{ title: 'Your cart' }} />
          <Stack.Screen name="checkout" options={{ title: 'Checkout' }} />
          <Stack.Screen name="payment" options={{ title: 'Payment', headerBackVisible: false }} />
          <Stack.Screen name="orders" options={{ title: 'My orders' }} />
          <Stack.Screen name="profile" options={{ title: 'Profile' }} />
          <Stack.Screen name="order/[id]" options={{ title: 'Order' }} />
          <Stack.Screen name="auth/phone" options={{ title: 'Sign in', presentation: 'modal' }} />
          <Stack.Screen name="auth/otp" options={{ title: 'Verify' }} />
        </Stack>
      </SafeAreaProvider>
    </QueryClientProvider>
  );
}
