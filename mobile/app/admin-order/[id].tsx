import { useState } from 'react';
import { Alert, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useLocalSearchParams } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { ownerApi } from '../../src/api/owner';
import { useSession } from '../../src/store/session';
import { formatPaise } from '../../src/lib/money';
import { shortDateTime } from '../../src/lib/time';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { OrderStatus, OrderView } from '../../src/api/types';
export { AppErrorFallback as ErrorBoundary } from '../../src/components/ErrorBoundary';

const STATUS_TITLE: Record<OrderStatus, string> = {
  pending_payment: 'Waiting for payment',
  confirmed: 'Preparing',
  packed: 'Packed',
  out_for_delivery: 'On the way',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  refunded: 'Refunded',
};

const CANCELLABLE: OrderStatus[] = ['pending_payment', 'confirmed', 'packed', 'out_for_delivery'];

/**
 * AAD-BIZ-006: one order, as the owner/staff see it. Customers can no longer
 * cancel from their app, so this is where an order gets cancelled — owner
 * only (the backend refuses staff; the button just isn't shown to them).
 * Cancelling a paid online order queues the full refund automatically; the
 * backend's sweep sends it to Razorpay within a couple of minutes, and
 * alerts the owner if it's still stuck after 30 (AAD-PAY-021).
 */
export default function AdminOrderScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const queryClient = useQueryClient();
  const isAdmin = useSession((s) => s.user?.role) === 'admin';
  const [reason, setReason] = useState('');
  const [showCancel, setShowCancel] = useState(false);

  const order = useQuery({
    queryKey: ['owner', 'order', id],
    queryFn: () => ownerApi.order(id),
    refetchInterval: (q) =>
      q.state.data?.payment.status === ('refund_pending' as string) ? 20_000 : false,
  });

  const cancel = useMutation({
    mutationFn: () => ownerApi.cancelOrder(id, reason.trim()),
    onSuccess: (updated) => {
      queryClient.setQueryData(['owner', 'order', id], updated);
      setShowCancel(false);
      setReason('');
      void queryClient.invalidateQueries({ queryKey: ['owner'] });
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  if (order.isPending) return <Loading />;
  if (order.isError) return <ErrorState error={order.error} onRetry={() => void order.refetch()} />;

  const data: OrderView = order.data;
  const paidOnline = data.payment.method === 'online' && data.payment.status === 'captured';
  const canCancel = isAdmin && CANCELLABLE.includes(data.status);
  const cancelLabel = paidOnline ? `Cancel & refund ${formatPaise(data.total_paise)}` : 'Cancel order';

  const confirmCancel = () => {
    if (!reason.trim()) return;
    Alert.alert(
      paidOnline ? 'Cancel and refund?' : 'Cancel this order?',
      paidOnline
        ? `Order #${data.order_number} will be cancelled and ${formatPaise(data.total_paise)} refunded to the customer's original payment method. This can't be undone.`
        : `Order #${data.order_number} will be cancelled. This can't be undone.`,
      [
        { text: 'Keep order', style: 'cancel' },
        { text: paidOnline ? 'Cancel & refund' : 'Cancel order', style: 'destructive', onPress: () => cancel.mutate() },
      ],
    );
  };

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
      <View style={styles.hero}>
        <Text variant="caption">Order #{data.order_number} · {shortDateTime(data.created_at)}</Text>
        <Text variant="title" style={styles.status}>{STATUS_TITLE[data.status] ?? data.status}</Text>
        <PaymentLine order={data} />
      </View>

      <View style={styles.card}>
        <Text variant="label" style={styles.cardTitle}>Items</Text>
        {data.lines.map((line) => (
          <View key={line.sku} style={styles.line}>
            <View style={styles.lineBody}>
              <Text variant="body" numberOfLines={1}>{line.product_name}</Text>
              <Text variant="caption">{line.variant_label} × {line.qty}</Text>
            </View>
            <Text variant="priceSmall" style={styles.ink}>{formatPaise(line.line_total_paise)}</Text>
          </View>
        ))}
        <View style={styles.divider} />
        <View style={styles.line}>
          <Text variant="body">Delivery</Text>
          <Text variant="priceSmall" style={styles.ink}>
            {data.delivery_fee_paise === 0 ? 'Free' : formatPaise(data.delivery_fee_paise)}
          </Text>
        </View>
        <View style={styles.line}>
          <Text variant="label" style={styles.totalLabel}>Total</Text>
          <Text style={styles.totalValue}>{formatPaise(data.total_paise)}</Text>
        </View>
      </View>

      <View style={styles.card}>
        <Text variant="label" style={styles.cardTitle}>Delivering to</Text>
        <Text variant="body">{data.address.line1}</Text>
        {data.address.line2 ? <Text variant="caption">{data.address.line2}</Text> : null}
        {data.address.landmark ? <Text variant="caption">Near {data.address.landmark}</Text> : null}
        <Text variant="caption">{data.address.city} {data.address.pincode}</Text>
        {data.notes ? <Text variant="caption" style={styles.notes}>Note: {data.notes}</Text> : null}
      </View>

      {canCancel && !showCancel && (
        <Button label={cancelLabel} variant="secondary" onPress={() => setShowCancel(true)} />
      )}

      {canCancel && showCancel && (
        <View style={styles.card}>
          <Text variant="label" style={styles.cardTitle}>{cancelLabel}</Text>
          <Text variant="caption">
            Why is this order being cancelled? This is saved on the order.
          </Text>
          <TextInput
            value={reason}
            onChangeText={setReason}
            placeholder="e.g. Out of stock, customer called to cancel"
            maxLength={200}
            style={styles.input}
            placeholderTextColor={color.muted}
          />
          {cancel.isError && (
            <Text style={styles.error}>
              {cancel.error instanceof Error ? cancel.error.message : 'Could not cancel. Try again.'}
            </Text>
          )}
          <Button
            label={cancelLabel}
            loading={cancel.isPending}
            disabled={!reason.trim()}
            onPress={confirmCancel}
          />
          <Button
            label="Keep order"
            variant="ghost"
            disabled={cancel.isPending}
            onPress={() => {
              setShowCancel(false);
              setReason('');
            }}
          />
        </View>
      )}
    </ScrollView>
  );
}

function PaymentLine({ order }: { order: OrderView }) {
  const status = order.payment.status as string;
  const amount = formatPaise(order.payment.amount_paise);
  if (order.payment.method === 'cod') {
    return <Text variant="caption">Cash on delivery · {amount}</Text>;
  }
  if (status === 'refund_pending') {
    return (
      <Text variant="caption" style={styles.warn}>
        Refund of {amount} is being sent to Razorpay…
      </Text>
    );
  }
  if (status === 'refunded') {
    return (
      <Text variant="caption" style={styles.good}>
        Refunded {amount}. It usually reaches the customer in 5–7 working days.
      </Text>
    );
  }
  if (status === 'captured') return <Text variant="caption" style={styles.good}>Paid online · {amount}</Text>;
  return <Text variant="caption">Online payment · {status.replace('_', ' ')}</Text>;
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  hero: { gap: space.xs },
  status: { fontSize: size.xl },
  card: { backgroundColor: color.card, borderRadius: radius.md, padding: space.lg, gap: space.sm },
  cardTitle: { fontSize: size.base },
  line: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: space.md },
  lineBody: { flex: 1, gap: 2 },
  ink: { color: color.ink },
  divider: { height: 1, backgroundColor: color.line, marginVertical: space.xs },
  totalLabel: { fontSize: size.md },
  totalValue: { fontFamily: font.monoBold, fontSize: size.md, color: color.ink },
  notes: { fontStyle: 'italic' },
  input: {
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 48,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
  },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  warn: { color: color.lowStock, fontFamily: font.bodyMedium },
  good: { color: color.leaf, fontFamily: font.bodyMedium },
});
