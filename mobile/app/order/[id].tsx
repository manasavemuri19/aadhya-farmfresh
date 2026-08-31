import { useEffect, useState } from 'react';
import { Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { ordersApi } from '../../src/api/endpoints';
import { formatPaise } from '../../src/lib/money';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { OrderStatus, OrderView } from '../../src/api/types';

const STEPS: { status: OrderStatus; label: string }[] = [
  { status: 'confirmed', label: 'Confirmed' },
  { status: 'packed', label: 'Packed' },
  { status: 'out_for_delivery', label: 'On the way' },
  { status: 'delivered', label: 'Delivered' },
];

const COPY: Record<OrderStatus, string> = {
  pending_payment: 'Waiting for payment',
  confirmed: 'The farm is preparing your order',
  packed: 'Packed and ready to leave',
  out_for_delivery: 'On the way to you',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  refunded: 'Refunded',
};

// Ticks so the ETA line below counts down in real time instead of freezing
// at whatever `eta_minutes` was when the order was placed. 30s is plenty —
// the line only ever displays whole minutes, so anything shorter is wasted
// re-renders.
function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/**
 * `eta_minutes` is a single number frozen on the order at checkout — the
 * slowest item's prep time (see pricing.compute_eta_minutes on the backend).
 * It is a promise made at confirmation time, not a live GPS/traffic ETA, so
 * the honest way to show it is as a countdown from the moment the order was
 * confirmed, not as a static "X minutes" that never changes. Once the
 * countdown runs out we stop naming a number — there's no live agent
 * location feed yet to say anything more precise than "any moment now."
 */
function etaText(data: OrderView, now: number): string | null {
  if (data.status === 'delivered' || data.status === 'cancelled' || data.status === 'refunded') {
    return null;
  }
  const confirmedAt = data.timeline.find((event) => event.status === 'confirmed')?.at;
  if (!confirmedAt) {
    // Still pending_payment — nothing confirmed yet to count down from.
    return `Arriving in about ${data.eta_minutes} minutes`;
  }
  const targetMs = new Date(confirmedAt).getTime() + data.eta_minutes * 60_000;
  const remainingMinutes = Math.round((targetMs - now) / 60_000);
  if (remainingMinutes >= 1) {
    return `Arriving in about ${remainingMinutes} minute${remainingMinutes === 1 ? '' : 's'}`;
  }
  return 'Arriving any moment now';
}

export default function OrderScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const now = useNow(30_000);

  const order = useQuery({
    queryKey: ['order', id],
    queryFn: () => ordersApi.get(id),
    // Poll while the order is live. The webhook may confirm payment a moment
    // after the app returns from the gateway, so the screen catches up on its
    // own rather than leaving the customer to pull-to-refresh.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status) return 5_000;
      return ['delivered', 'cancelled', 'refunded'].includes(status) ? false : 5_000;
    },
  });

  const cancel = useMutation({
    mutationFn: (reason: string) => ordersApi.cancel(id, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['order', id] });
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  if (order.isPending) return <Loading />;
  if (order.isError) {
    return (
      <ErrorState
        message={order.error instanceof Error ? order.error.message : 'Try again.'}
        onRetry={() => void order.refetch()}
      />
    );
  }

  const data: OrderView = order.data;
  const currentStep = STEPS.findIndex((s) => s.status === data.status);
  const stopped = data.status === 'cancelled' || data.status === 'refunded';
  const eta = etaText(data, now);

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <View style={styles.hero}>
        <Text variant="caption">Order {data.order_number}</Text>
        <Text variant="title" style={styles.status}>{COPY[data.status]}</Text>
        {eta && (
          <Text variant="caption" style={styles.eta}>
            {eta}
          </Text>
        )}
      </View>

      {!stopped && (
        <View style={styles.track}>
          {STEPS.map((step, index) => {
            const done = currentStep >= index && currentStep !== -1;
            return (
              <View key={step.status} style={styles.trackStep}>
                <View style={[styles.dot, done && styles.dotDone]} />
                <Text style={[styles.trackLabel, done && styles.trackLabelDone]}>
                  {step.label}
                </Text>
              </View>
            );
          })}
        </View>
      )}

      <View style={styles.card}>
        <Text variant="label" style={styles.cardTitle}>Items</Text>
        {data.lines.map((line: OrderView['lines'][number]) => (
          <View key={line.sku} style={styles.line}>
            <View style={styles.lineBody}>
              <Text variant="body" numberOfLines={1}>{line.product_name}</Text>
              <Text variant="caption">
                {line.variant_label} × {line.qty}
              </Text>
            </View>
            <Text variant="priceSmall" style={styles.lineTotal}>
              {formatPaise(line.line_total_paise)}
            </Text>
          </View>
        ))}

        <View style={styles.divider} />
        <View style={styles.line}>
          <Text variant="body">Delivery</Text>
          <Text variant="priceSmall" style={styles.lineTotal}>
            {data.delivery_fee_paise === 0 ? 'Free' : formatPaise(data.delivery_fee_paise)}
          </Text>
        </View>
        <View style={styles.line}>
          <Text variant="label" style={styles.totalLabel}>Total</Text>
          <Text style={styles.totalValue}>{formatPaise(data.total_paise)}</Text>
        </View>
        <Text variant="caption">
          {data.payment.method === 'cod' ? 'Paying on delivery' : 'Paid online'}
        </Text>
      </View>

      <View style={styles.card}>
        <Text variant="label" style={styles.cardTitle}>Delivering to</Text>
        <Text variant="body">{data.address.line1}</Text>
        {data.address.landmark ? (
          <Text variant="caption">{data.address.landmark}</Text>
        ) : null}
        <Text variant="caption">
          {data.address.city} {data.address.pincode}
        </Text>
        {data.can_edit_address && (
          <Pressable onPress={() => router.push(`/order-edit-address?orderId=${data.id}`)}>
            <Text style={styles.editAddressLink}>Edit delivery address</Text>
          </Pressable>
        )}
      </View>

      {data.can_cancel && (
        <Button
          label="Cancel this order"
          variant="secondary"
          loading={cancel.isPending}
          onPress={() => cancel.mutate('Cancelled from the app')}
        />
      )}

      {cancel.isError && (
        <Text style={styles.error}>
          {cancel.error instanceof Error ? cancel.error.message : 'Could not cancel.'}
        </Text>
      )}

      <Button
        label="Back to shop"
        variant="ghost"
        onPress={() => router.replace('/')}
      />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  hero: { gap: space.xs },
  status: { fontSize: size.xl },
  eta: { color: color.leaf, fontFamily: font.bodyMedium },
  track: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
  },
  trackStep: { alignItems: 'center', gap: space.sm, flex: 1 },
  dot: { width: 12, height: 12, borderRadius: 6, backgroundColor: color.line },
  dotDone: { backgroundColor: color.leaf },
  trackLabel: { fontFamily: font.body, fontSize: size.xs, color: color.muted, textAlign: 'center' },
  trackLabelDone: { color: color.ink, fontFamily: font.bodyMedium },
  card: { backgroundColor: color.card, borderRadius: radius.md, padding: space.lg, gap: space.sm },
  cardTitle: { fontSize: size.base },
  editAddressLink: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.primary, marginTop: 2 },
  line: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: space.md },
  lineBody: { flex: 1, gap: 2 },
  lineTotal: { color: color.ink },
  divider: { height: 1, backgroundColor: color.line, marginVertical: space.xs },
  totalLabel: { fontSize: size.md },
  totalValue: { fontFamily: font.monoBold, fontSize: size.md, color: color.ink },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
});
