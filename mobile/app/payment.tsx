import { useState } from 'react';
import { Linking, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { ErrorState, Loading } from '../src/components/Feedback';
import { ordersApi, paymentsApi } from '../src/api/endpoints';
import { formatPaise } from '../src/lib/money';
import { color, font, radius, size, space } from '../src/theme/tokens';

/**
 * Two real paths, chosen by `order.payment.provider`:
 *
 *  - "razorpay": opens the actual Razorpay-hosted Payment Link in the
 *    device's browser via the plain `Linking` API — nothing native to add,
 *    so this ships and updates over `eas update`, not a rebuild. Razorpay
 *    redirects back to this app's own `aadhya://payment-callback` scheme
 *    once the customer pays, landing on app/payment-callback.tsx.
 *
 *  - "mock": no gateway account exists behind this provider, so there is no
 *    real page to send anyone to — these buttons stand in for what would
 *    otherwise be "the customer completed checkout on Razorpay's page."
 */
export default function PaymentScreen() {
  const { orderId } = useLocalSearchParams<{ orderId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [failed, setFailed] = useState(false);

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

  const shortUrl = order.data.payment.checkout_payload?.short_url as string | undefined;
  const isRealProvider = order.data.payment.provider === 'razorpay';

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
          <Button
            label="Pay now"
            disabled={!shortUrl}
            onPress={() => shortUrl && Linking.openURL(shortUrl)}
          />
          <Text variant="caption" style={styles.note}>
            Opens Razorpay's secure payment page. You'll be brought back here
            automatically once it's done.
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
