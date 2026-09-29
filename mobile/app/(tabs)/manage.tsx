import { ActivityIndicator, FlatList, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { EmptyState, ErrorState, Loading } from '../../src/components/Feedback';
import { ownerApi, type RefundPending } from '../../src/api/owner';
import { useSession } from '../../src/store/session';
import { formatPaise } from '../../src/lib/money';
import { sinceText } from '../../src/lib/time';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { OrderStatus, OrderView } from '../../src/api/types';

const STATUS_LABEL: Partial<Record<OrderStatus, string>> = {
  confirmed: 'Preparing',
  packed: 'Packed',
  out_for_delivery: 'On the way',
};

/**
 * AAD-BIZ-006: the owner/staff live order queue. Visible to staff and admin
 * (see (tabs)/_layout.tsx); tapping an order opens admin-order/[id], where
 * only the owner gets "Cancel & refund".
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

  const queue = useInfiniteQuery({
    queryKey: ['owner', 'queue'],
    queryFn: ({ pageParam }: { pageParam?: string }) => ownerApi.orderQueue(pageParam),
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

  if (queue.isPending) return <Loading label="Loading orders" />;
  if (queue.isError) return <ErrorState error={queue.error} onRetry={() => void queue.refetch()} />;

  const items = queue.data.pages.flatMap((p) => p.items);
  const pendingRefunds = refunds.data ?? [];

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
          <Text variant="display" style={styles.heading}>Orders</Text>
          {isAdmin && pendingRefunds.length > 0 && (
            <View style={styles.refundBlock}>
              <Text variant="label" style={styles.sectionLabel}>Refunds in progress</Text>
              {pendingRefunds.map((r) => (
                <RefundRow key={r.order_id} refund={r} onPress={() => router.push(`/admin-order/${r.order_id}`)} />
              ))}
            </View>
          )}
          <Text variant="label" style={styles.sectionLabel}>Live orders</Text>
        </View>
      }
      renderItem={({ item }) => (
        <OrderRow order={item} onPress={() => router.push(`/admin-order/${item.id}`)} />
      )}
      ListEmptyComponent={
        <EmptyState title="No live orders" message="New paid and COD orders will show up here." />
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

function OrderRow({ order, onPress }: { order: OrderView; onPress: () => void }) {
  const itemCount = order.lines.reduce((n, l) => n + l.qty, 0);
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      style={({ pressed }) => [styles.card, pressed && styles.pressed]}
    >
      <View style={styles.rowTop}>
        <Text style={styles.orderNumber}>#{order.order_number}</Text>
        <Text style={styles.status}>{STATUS_LABEL[order.status] ?? order.status}</Text>
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
    </Pressable>
  );
}

const styles = StyleSheet.create({
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
