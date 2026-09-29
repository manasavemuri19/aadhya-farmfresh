import { useState } from 'react';
import { ActivityIndicator, FlatList, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { EmptyState, ErrorState, Loading } from '../../src/components/Feedback';
import { ownerApi, type OrderFilter, type RefundPending, type StaffOrder } from '../../src/api/owner';
import { useSession } from '../../src/store/session';
import { formatPaise } from '../../src/lib/money';
import { sinceText } from '../../src/lib/time';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { OrderStatus } from '../../src/api/types';

const STATUS_LABEL: Partial<Record<OrderStatus, string>> = {
  confirmed: 'Preparing',
  packed: 'Packed',
  out_for_delivery: 'On the way',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  refunded: 'Refunded',
};

const FILTERS: { key: OrderFilter; label: string; empty: string }[] = [
  { key: 'live', label: 'Live', empty: 'New paid and COD orders will show up here.' },
  { key: 'delivered', label: 'Delivered', empty: 'Delivered orders will show up here.' },
  { key: 'closed', label: 'Cancelled', empty: 'Cancelled and refunded orders will show up here.' },
];

/**
 * AAD-BIZ-006: the owner's order screen. Owner-only (see (tabs)/_layout.tsx;
 * there is no separate staff role since AAD-BIZ-007). Three views: Live (the work queue), Delivered, and
 * Cancelled — a delivered order used to simply vanish from here, leaving no
 * way to see it, refund it, or check where a COD order's cash ended up.
 * Tapping an order opens admin-order/[id], where only the owner gets
 * "Cancel & refund" / "Refund".
 *
 * AAD-PAY-021: for the owner, refunds still waiting on Razorpay sit at the
 * top, red once they're past the 30-minute alert threshold, until they go
 * through.
 *
 * Query keys are namespaced under 'owner' — never a bare ['orders'], which
 * the customer screens use (sharing a key with a different data shape is
 * exactly what broke "My orders"; see AAD-MOB-030).
 */
export default function ManageOrdersScreen() {
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const isAdmin = useSession((s) => s.user?.role) === 'admin';
  const [filter, setFilter] = useState<OrderFilter>('live');

  const queue = useInfiniteQuery({
    queryKey: ['owner', 'queue', filter],
    queryFn: ({ pageParam }: { pageParam?: string }) => ownerApi.orderList(filter, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => (last.has_more ? last.next_cursor ?? undefined : undefined),
    refetchInterval: 30_000,
  });

  const refunds = useQuery({
    queryKey: ['owner', 'refunds'],
    queryFn: () => ownerApi.refundsPending(),
    enabled: isAdmin,
    refetchInterval: 60_000,
  });

  const tabs = (
    <View>
      <Text variant="display" style={styles.heading}>Orders</Text>
      {/* AAD-BIZ-007: the "signed in as staff" note this used to show is
          gone with the staff role — only the owner reaches this screen.
      {!isAdmin && (
        <View style={styles.roleNote}>
          <Text variant="caption" style={styles.roleNoteText}>
            Signed in as {role === 'staff' ? 'staff' : role ?? 'staff'}. Cancel &amp; refund and the
            Cash tab are only available on the owner account.
          </Text>
        </View>
      )}
      */}
      <View style={styles.filters}>
        {FILTERS.map((f) => (
          <Pressable
            key={f.key}
            onPress={() => setFilter(f.key)}
            accessibilityRole="button"
            accessibilityState={{ selected: filter === f.key }}
            style={[styles.filterChip, filter === f.key && styles.filterChipOn]}
          >
            <Text style={[styles.filterText, filter === f.key && styles.filterTextOn]}>{f.label}</Text>
          </Pressable>
        ))}
      </View>
    </View>
  );

  if (queue.isPending) {
    return (
      <View style={[styles.screen, styles.content, { paddingTop: insets.top + space.lg }]}>
        {tabs}
        <Loading label="Loading orders" />
      </View>
    );
  }
  if (queue.isError) {
    return (
      <View style={[styles.screen, styles.content, { paddingTop: insets.top + space.lg }]}>
        {tabs}
        <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />
      </View>
    );
  }

  const items = queue.data.pages.flatMap((p) => p.items);
  const pendingRefunds = refunds.data ?? [];
  const currentFilter = FILTERS.find((f) => f.key === filter) ?? FILTERS[0]!;

  return (
    <FlatList
      style={styles.screen}
      contentContainerStyle={[styles.content, { paddingTop: insets.top + space.lg }]}
      data={items}
      keyExtractor={(o) => o.id}
      refreshing={queue.isRefetching && !queue.isFetchingNextPage}
      onRefresh={() => {
        void queue.refetch();
        if (isAdmin) void refunds.refetch();
      }}
      onEndReachedThreshold={0.4}
      onEndReached={() => {
        if (queue.hasNextPage && !queue.isFetchingNextPage) void queue.fetchNextPage();
      }}
      ListHeaderComponent={
        <View>
          {tabs}
          {isAdmin && pendingRefunds.length > 0 && (
            <View style={styles.refundBlock}>
              <Text variant="label" style={styles.sectionLabel}>Refunds in progress</Text>
              {pendingRefunds.map((r) => (
                <RefundRow key={r.order_id} refund={r} onPress={() => router.push(`/admin-order/${r.order_id}`)} />
              ))}
            </View>
          )}
          <Text variant="label" style={styles.sectionLabel}>{currentFilter.label} orders</Text>
        </View>
      }
      renderItem={({ item }) => (
        <OrderRow order={item} onPress={() => router.push(`/admin-order/${item.id}`)} />
      )}
      ListEmptyComponent={
        <EmptyState title={`No ${currentFilter.label.toLowerCase()} orders`} message={currentFilter.empty} />
      }
      ListFooterComponent={
        queue.isFetchingNextPage ? <ActivityIndicator color={color.leaf} style={styles.footer} /> : null
      }
    />
  );
}

function RefundRow({ refund, onPress }: { refund: RefundPending; onPress: () => void }) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      style={({ pressed }) => [styles.refundRow, refund.stuck && styles.refundRowStuck, pressed && styles.pressed]}
    >
      <View style={styles.rowBody}>
        <Text style={styles.orderNumber}>#{refund.order_number}</Text>
        <Text variant="caption" style={refund.stuck ? styles.stuckText : undefined}>
          {refund.stuck
            ? `Stuck for ${sinceText(refund.pending_since)} — check Razorpay`
            : `Refund queued ${sinceText(refund.pending_since)} ago`}
        </Text>
      </View>
      <Text style={styles.amount}>{formatPaise(refund.amount_paise)}</Text>
    </Pressable>
  );
}

function OrderRow({ order, onPress }: { order: StaffOrder; onPress: () => void }) {
  const itemCount = order.lines.reduce((n, l) => n + l.qty, 0);
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      style={({ pressed }) => [styles.card, pressed && styles.pressed]}
    >
      <View style={styles.rowTop}>
        <Text style={styles.orderNumber}>#{order.order_number}</Text>
        <Text style={[styles.status, isClosed(order.status) && styles.statusClosed]}>
          {STATUS_LABEL[order.status] ?? order.status}
        </Text>
      </View>
      <Text variant="caption" numberOfLines={1}>
        {order.address.line1}
        {order.address.landmark ? `, near ${order.address.landmark}` : ''}
      </Text>
      <View style={styles.rowTop}>
        <Text variant="caption">
          {itemCount} item{itemCount === 1 ? '' : 's'} ·{' '}
          {order.payment.method === 'cod' ? 'Cash on delivery' : 'Paid online'} ·{' '}
          {sinceText(order.created_at)} ago
        </Text>
        <Text style={styles.amount}>{formatPaise(order.total_paise)}</Text>
      </View>
      {order.cod_cash && (
        <Text variant="caption" style={order.cod_cash.settled ? styles.cashSettled : styles.cashPending}>
          {order.cod_cash.settled
            ? `Cash ${formatPaise(order.cod_cash.amount_paise)} received from ${order.cod_cash.agent_name ?? 'delivery partner'}`
            : `Cash ${formatPaise(order.cod_cash.amount_paise)} with ${order.cod_cash.agent_name ?? 'delivery partner'} — not handed over yet`}
        </Text>
      )}
    </Pressable>
  );
}

function isClosed(status: OrderStatus): boolean {
  return status === 'cancelled' || status === 'refunded';
}

const styles = StyleSheet.create({
  filters: { flexDirection: 'row', gap: space.sm, marginBottom: space.sm },
  filterChip: {
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderRadius: radius.pill,
    backgroundColor: color.card,
    borderWidth: 1,
    borderColor: color.line,
  },
  filterChipOn: { backgroundColor: color.primary, borderColor: color.primary },
  filterText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.body },
  filterTextOn: { color: color.onPrimary },
  roleNote: {
    backgroundColor: color.primarySoft,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.sm,
  },
  roleNoteText: { color: color.primaryPressed },
  statusClosed: { color: color.muted },
  cashPending: { color: color.lowStock, fontFamily: font.bodyMedium },
  cashSettled: { color: color.leaf, fontFamily: font.bodyMedium },
  screen: { flex: 1, backgroundColor: color.surface },
  content: { paddingHorizontal: space.lg, paddingBottom: space.xxl },
  heading: { marginBottom: space.md },
  sectionLabel: { marginTop: space.sm, marginBottom: space.sm, fontSize: size.base },
  refundBlock: { marginBottom: space.sm },
  refundRow: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.sm,
    borderWidth: 1,
    borderColor: color.line,
  },
  refundRowStuck: { borderColor: color.discount, backgroundColor: color.discountSoft },
  stuckText: { color: color.discount, fontFamily: font.bodyMedium },
  card: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.sm,
    borderWidth: 1,
    borderColor: color.line,
    gap: 4,
  },
  pressed: { opacity: 0.85 },
  rowBody: { flex: 1, gap: 2 },
  rowTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: space.sm },
  orderNumber: { fontFamily: font.bodyBold, fontSize: size.base, color: color.ink },
  status: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.leaf },
  amount: { fontFamily: font.monoBold, fontSize: size.base, color: color.ink },
  footer: { marginVertical: space.lg },
});
