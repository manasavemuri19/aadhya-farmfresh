import { useState } from 'react';
import { Alert, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useLocalSearchParams } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { ownerApi, type StaffOrder } from '../../src/api/owner';
import { useSession } from '../../src/store/session';
import { formatPaise } from '../../src/lib/money';
import { shortDateTime } from '../../src/lib/time';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { OrderStatus } from '../../src/api/types';
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
 * AAD-BIZ-006: one order, as the owner sees it. Customers can no longer
 * cancel from their app, so this is where an order gets cancelled — owner
 * only (AAD-BIZ-007: there is no staff role; the backend refuses everyone
 * else regardless of what this screen shows).
 *
 *  - Before delivery: "Cancel & refund" (paid online → full refund is queued)
 *    or "Cancel order" (COD / not yet paid — no money to send back).
 *  - After delivery: "Refund" for a paid online order (spoiled milk, wrong
 *    item) — nothing goes back on the shelf. A delivered COD order instead
 *    shows where its cash is; refunding cash is done in person, outside the
 *    app.
 *
 * The backend's sweep sends refunds to Razorpay within a couple of minutes,
 * and alerts the owner if one is still stuck after 30 (AAD-PAY-021).
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
    mutationFn: (mode: 'cancel' | 'refund') =>
      mode === 'refund' ? ownerApi.refundOrder(id, reason.trim()) : ownerApi.cancelOrder(id, reason.trim()),
    onSuccess: () => {
      setShowCancel(false);
      setReason('');
      // Refetch rather than patch: the status route's response is the plain
      // order, without the COD cash info this screen also shows.
      void queryClient.invalidateQueries({ queryKey: ['owner'] });
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
    },
  });

  if (order.isPending) return <Loading />;
  if (order.isError) return <ErrorState error={order.error} onRetry={() => void order.refetch()} />;

  const data: StaffOrder = order.data;
  const paidOnline = data.payment.method === 'online' && data.payment.status === 'captured';
  const isDelivered = data.status === 'delivered';
  // Pre-delivery: cancel (with a refund attached when it was paid online).
  // Post-delivery: refund — only where there's online money to send back.
  const canCancel = isAdmin && CANCELLABLE.includes(data.status);
  const canRefundDelivered = isAdmin && isDelivered && paidOnline;
  const canAct = canCancel || canRefundDelivered;
  const actionLabel = canRefundDelivered
    ? `Refund ${formatPaise(data.total_paise)}`
    : paidOnline
      ? `Cancel & refund ${formatPaise(data.total_paise)}`
      : 'Cancel order';
  const cancelLabel = actionLabel;

  const confirmCancel = () => {
    if (!reason.trim()) return;
    Alert.alert(
      canRefundDelivered ? 'Refund this order?' : paidOnline ? 'Cancel and refund?' : 'Cancel this order?',
      canRefundDelivered
        ? `${formatPaise(data.total_paise)} will be refunded to the customer's original payment method for order #${data.order_number}. This can't be undone.`
        : paidOnline
          ? `Order #${data.order_number} will be cancelled and ${formatPaise(data.total_paise)} refunded to the customer's original payment method. This can't be undone.`
          : `Order #${data.order_number} will be cancelled. This can't be undone.`,
      [
        { text: 'Keep order', style: 'cancel' },
        {
          text: canRefundDelivered ? 'Refund' : paidOnline ? 'Cancel & refund' : 'Cancel order',
          style: 'destructive',
          onPress: () => cancel.mutate(canRefundDelivered ? 'refund' : 'cancel'),
        },
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

      {data.cod_cash && (
        <View style={styles.card}>
          <Text variant="label" style={styles.cardTitle}>Cash on delivery</Text>
          <Text
            variant="body"
            style={data.cod_cash.settled ? styles.good : styles.warn}
          >
            {data.cod_cash.settled
              ? `${formatPaise(data.cod_cash.amount_paise)} received from ${data.cod_cash.agent_name ?? 'the delivery partner'}.`
              : `${formatPaise(data.cod_cash.amount_paise)} collected by ${data.cod_cash.agent_name ?? 'the delivery partner'} — not handed over yet.`}
          </Text>
          <Text variant="caption">
            {data.cod_cash.settled
              ? 'Settled on the Cash tab.'
              : 'Mark it received on the Cash tab once they hand it to you.'}
          </Text>
        </View>
      )}

      {/* AAD-BIZ-007: the "only available on the owner account" hint that
          used to sit here is gone with the staff role.
      {!isAdmin && (data.status === 'delivered' || CANCELLABLE.includes(data.status)) && (
        <Text variant="caption" style={styles.roleNote}>
          Cancel &amp; refund is only available on the owner account.
        </Text>
      )}
      */}

      {isAdmin && isDelivered && !paidOnline && data.payment.method === 'cod' && (
        <Text variant="caption" style={styles.roleNote}>
          Cash on delivery orders are refunded in cash, in person — there's nothing to send back
          through Razorpay.
        </Text>
      )}

      {canAct && !showCancel && (
        <Button label={actionLabel} variant="secondary" onPress={() => setShowCancel(true)} />
      )}

      {canAct && showCancel && (
        <View style={styles.card}>
          <Text variant="label" style={styles.cardTitle}>{cancelLabel}</Text>
          <Text variant="caption">
            {canRefundDelivered
              ? 'Why is this order being refunded? This is saved on the order.'
              : 'Why is this order being cancelled? This is saved on the order.'}
          </Text>
          <TextInput
            value={reason}
            onChangeText={setReason}
            placeholder={
              canRefundDelivered ? 'e.g. Milk arrived spoiled' : 'e.g. Out of stock, customer called to cancel'
            }
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

function PaymentLine({ order }: { order: StaffOrder }) {
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
  roleNote: { color: color.muted },
  warn: { color: color.lowStock, fontFamily: font.bodyMedium },
  good: { color: color.leaf, fontFamily: font.bodyMedium },
});
