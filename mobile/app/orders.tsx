import { FlatList, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useQuery } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { EmptyState, ErrorState, Loading } from '../src/components/Feedback';
import { ordersApi } from '../src/api/endpoints';
import { useSession } from '../src/store/session';
import { formatPaise } from '../src/lib/money';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { OrderStatus, OrderView } from '../src/api/types';

const LABEL: Record<OrderStatus, string> = {
  pending_payment: 'Awaiting payment',
  confirmed: 'Preparing',
  packed: 'Packed',
  out_for_delivery: 'On the way',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  refunded: 'Refunded',
};

export default function OrdersScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const status = useSession((s) => s.status);

  const orders = useQuery({
    queryKey: ['orders'],
    queryFn: () => ordersApi.list(),
    enabled: status === 'signed_in',
  });

  if (status === 'signed_out') {
    return (
      <EmptyState
        title="Sign in to see your orders"
        message="Your past orders and live deliveries will show up here."
        actionLabel="Sign in"
        onAction={() => router.push('/auth/phone')}
      />
    );
  }

  if (orders.isPending) return <Loading />;
  if (orders.isError) {
    return (
      <ErrorState
        message={orders.error instanceof Error ? orders.error.message : 'Try again.'}
        onRetry={() => void orders.refetch()}
      />
    );
  }

  if (orders.data.length === 0) {
    return (
      <EmptyState
        title="No orders yet"
        message="Once you order, you can track it here."
        actionLabel="Start shopping"
        onAction={() => router.replace('/')}
      />
    );
  }

  return (
    <FlatList
      style={styles.screen}
      contentContainerStyle={[styles.list, { paddingTop: insets.top + space.lg }]}
      data={orders.data}
      keyExtractor={(order) => order.id}
      ListHeaderComponent={<Text variant="display" style={styles.heading}>Orders</Text>}
      refreshing={orders.isRefetching}
      onRefresh={() => void orders.refetch()}
      renderItem={({ item }: { item: OrderView }) => (
        <Pressable
          onPress={() => router.push(`/order/${item.id}`)}
          accessibilityRole="button"
          style={({ pressed }) => [styles.row, pressed && styles.pressed]}
        >
          <View style={styles.rowBody}>
            <Text variant="label">{item.order_number}</Text>
            <Text variant="caption">
              {item.lines.length} {item.lines.length === 1 ? 'item' : 'items'} ·{' '}
              {new Date(item.created_at).toLocaleDateString('en-IN', {
                day: 'numeric', month: 'short',
              })}
            </Text>
          </View>
          <View style={styles.rowEnd}>
            <Text variant="price" style={styles.amount}>{formatPaise(item.total_paise)}</Text>
            <Text style={styles.status}>{LABEL[item.status]}</Text>
          </View>
        </Pressable>
      )}
    />
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.sm },
  heading: { marginBottom: space.md },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
    marginBottom: space.sm,
  },
  pressed: { opacity: 0.9 },
  rowBody: { gap: 2, flex: 1 },
  rowEnd: { alignItems: 'flex-end', gap: 2 },
  amount: { fontSize: size.base },
  status: { fontFamily: font.bodyMedium, fontSize: size.xs, color: color.leaf },
});
