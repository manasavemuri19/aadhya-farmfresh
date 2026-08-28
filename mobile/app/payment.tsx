import { useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { ErrorState, Loading } from '../src/components/Feedback';
import { ordersApi, paymentsApi } from '../src/api/endpoints';
import { formatPaise } from '../src/lib/money';
import { color, font, radius, size, space } from '../src/theme/tokens';

/**
 * Stands in for the real gateway checkout sheet (Razorpay) until the
 * business's account is live. The order is already created server-side in
 * `pending_payment` — this screen's only job is to produce a
 * (provider_order_id, provider_payment_id, signature) triple and post it to
 * /payments/verify, exactly like the real SDK's success callback would.
 *
 * Swapping in real Razorpay later means replacing `simulate()` with a call
 * to Razorpay's Checkout SDK and using *its* callback values instead of the
 * mock-sign endpoint — `confirmPayment()` below and everything else on this
 * screen stays the same, since both paths end at the same /verify call.
 */
export default function PaymentScreen() {
  const { orderId } = useLocalSearchParams<{ orderId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const [failed, setFailed] = useState(false);

  const order = useQuery({
    queryKey: ['order', orderId],
    queryFn: () => ordersApi.get(orderId),
  });

  const confirmPayment = useMutation({
    mutationFn: (outcome: 'success' | 'failure') => paymentsApi.mockComplete(orderId, outcome),
    onSuccess: (order) => {
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
      if (order.status === 'confirmed') {
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

  if (order.data.payment.method !== 'online' || order.data.status !== 'pending_payment') {
    // Already paid, cash order, or already resolved some other way — nothing
    // for this screen to do.
    router.replace(`/order/${orderId}`);
    return null;
  }

  return (
    <View style={styles.screen}>
      <View style={styles.card}>
        <Text variant="caption">Amount to pay</Text>
        <Text style={styles.amount}>{formatPaise(order.data.total_paise)}</Text>
        <Text variant="caption" style={styles.note}>
          Test mode — standing in for the real payment screen until online
          payments go live. No real money moves here.
        </Text>
      </View>

      {failed && (
        <Text style={styles.error}>That payment did not go through. Try again.</Text>
      )}
      {confirmPayment.isError && (
        <Text style={styles.error}>
          {confirmPayment.error instanceof Error ? confirmPayment.error.message : 'Could not confirm payment.'}
        </Text>
      )}

      <Button
        label="Simulate successful payment"
        loading={confirmPayment.isPending}
        onPress={() => {
          setFailed(false);
          confirmPayment.mutate('success');
        }}
      />
      <Button
        label="Simulate failed payment"
        variant="secondary"
        disabled={confirmPayment.isPending}
        onPress={() => confirmPayment.mutate('failure')}
      />
      <Button
        label="Cancel and go back"
        variant="ghost"
        onPress={() => router.replace(`/order/${orderId}`)}
      />
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
