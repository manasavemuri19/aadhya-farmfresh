import { useEffect, useRef, useState } from 'react';
import { Linking, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { Loading, ErrorState } from '../src/components/Feedback';
import { ordersApi, paymentsApi } from '../src/api/endpoints';
import { formatPaise } from '../src/lib/money';
import { color, font, radius, size, space } from '../src/theme/tokens';

/**
 * Two real paths, chosen by `order.payment.provider`:
 *
 *  - "razorpay": opens Razorpay's hosted Payment Link automatically, the
 *    instant it's available — no manual "Pay now" tap required. This screen
 *    is a brief transit stop, not a destination: on arrival it immediately
 *    hands off to the device browser via `Linking.openURL`, nothing
 *    native to add, so this ships and updates over `eas update`. Razorpay
 *    redirects back to this app's own `aadhya://payment-callback` scheme
 *    once the customer pays, landing on app/payment-callback.tsx. A manual
 *    button stays as a fallback only, for the rare case the automatic
 *    handoff doesn't fire (e.g. the browser blocked the auto-open).
 *
 *  - "mock": no gateway account exists behind this provider, so there is no
 *    real page to send anyone to — these buttons stand in for what would
 *    otherwise be "the customer completed checkout on Razorpay's page."
 *
 * No transform/rotation styling exists anywhere in this file — if text
 * still renders flipped after replacing it with this exact file, the cause
 * is upstream of this screen (a shared component, a global style, or a
 * device-level RTL setting), not this code.
 */
export default function PaymentScreen() {
  const { orderId } = useLocalSearchParams<{ orderId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [failed, setFailed] = useState(false);
  const autoOpened = useRef(false);

  const order = useQuery({
    queryKey: ['order', orderId],
    queryFn: () => ordersApi.get(orderId),
    // The customer may be away in the browser for a while — keep checking
    // for the redirect-callback's confirmation without needing a manual
    // pull-to-refresh when they come back.
    refetchInterval: (query) => (query.state.data?.status === 'pending_payment' ? 4_000 : false),
  });

  const confirmMock = useMutation({
    mutationFn: (outcome: 'success' | 'failure') => paymentsApi.mockComplete(orderId, outcome),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
      if (result.status === 'confirmed') {
        router.replace(`/order/${orderId}`);
      } else {
        setFailed(true);
      }
    },
  });

  const shortUrl = order.data?.payment.checkout_payload?.short_url as string | undefined;
  const isRealProvider = order.data?.payment.provider === 'razorpay';

  // Fires once, the moment the payment link is ready — this is what removes
  // the extra manual tap. Guarded by a ref, not just state, so React's
  // render-twice-in-dev behaviour and the polling refetch above can never
  // reopen the browser a second time for the same order.
  useEffect(() => {
    if (isRealProvider && shortUrl && !autoOpened.current) {
      autoOpened.current = true;
      void Linking.openURL(shortUrl);
    }
  }, [isRealProvider, shortUrl]);

  if (order.isPending) return <Loading label="Preparing payment" />;
  if (order.isError || !order.data) {
    return <ErrorState message="Could not load this order." onRetry={() => void order.refetch()} />;
  }

  if (order.data.status === 'confirmed') {
    router.replace(`/order/${orderId}`);
    return null;
  }
  if (order.data.payment.method !== 'online' || order.data.status !== 'pending_payment') {
    router.replace(`/order/${orderId}`);
    return null;
  }

  return (
    <View style={styles.screen}>
      <View style={styles.card}>
        <Text variant="caption">Amount to pay</Text>
        <Text style={styles.amount}>{formatPaise(order.data.total_paise)}</Text>
        {!isRealProvider && (
          <Text variant="caption" style={styles.note}>
            Test mode — standing in for the real payment screen until online
            payments go live. No real money moves here.
          </Text>
        )}
      </View>

      {failed && <Text style={styles.error}>That payment did not go through. Try again.</Text>}

      {isRealProvider ? (
        <>
          <Text variant="caption" style={styles.note}>
            Opening Razorpay's secure payment page…
          </Text>
          <Button
            label="Open payment page"
            variant="secondary"
            disabled={!shortUrl}
            onPress={() => shortUrl && Linking.openURL(shortUrl)}
          />
          <Text variant="caption" style={styles.note}>
            Didn't open automatically? Tap the button above.
          </Text>
        </>
      ) : (
        <>
          <Button
            label="Simulate successful payment"
            loading={confirmMock.isPending}
            onPress={() => { setFailed(false); confirmMock.mutate('success'); }}
          />
          <Button
            label="Simulate failed payment"
            variant="secondary"
            disabled={confirmMock.isPending}
            onPress={() => confirmMock.mutate('failure')}
          />
        </>
      )}
      <Button label="Cancel and go back" variant="ghost" onPress={() => router.replace(`/order/${orderId}`)} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface, padding: space.lg, gap: space.md, justifyContent: 'center' },
  card: { backgroundColor: color.card, borderRadius: radius.md, padding: space.xl, alignItems: 'center', gap: space.xs },
  amount: { fontFamily: font.monoBold, fontSize: size.xxl, color: color.primary },
  note: { textAlign: 'center', marginTop: space.sm },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount, textAlign: 'center' },
});
