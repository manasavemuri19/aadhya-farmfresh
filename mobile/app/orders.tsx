import { ActivityIndicator, FlatList, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useInfiniteQuery } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { EmptyState, ErrorState, Loading } from '../src/components/Feedback';
import { ordersApi } from '../src/api/endpoints';
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
  // AAD-API-004: a customer used to be physically unable to see past their
  // 20 most recent orders — GET /orders exposed a limit but never the
  // cursor the repository already supported. This screen now walks that
  // cursor with react-query's own pagination primitive rather than
  // fetching everything at once.
  const orders = useInfiniteQuery({
    queryKey: ['orders'],
    queryFn: ({ pageParam }: { pageParam?: string }) => ordersApi.list(pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (lastPage) => (lastPage.has_more ? lastPage.next_cursor ?? undefined : undefined),
  });

  if (orders.isPending) return <Loading />;
  if (orders.isError) {
    return (
      <ErrorState error={orders.error} onRetry={() => void orders.refetch()} />
    );
  }

  const items = orders.data.pages.flatMap((page) => page.items);

  if (items.length === 0) {
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
      data={items}
      keyExtractor={(order) => order.id}
      ListHeaderComponent={<Text variant="display" style={styles.heading}>Orders</Text>}
      refreshing={orders.isRefetching && !orders.isFetchingNextPage}
      onRefresh={() => void orders.refetch()}
      onEndReachedThreshold={0.4}
      onEndReached={() => {
        if (orders.hasNextPage && !orders.isFetchingNextPage) void orders.fetchNextPage();
      }}
      ListFooterComponent={
        orders.isFetchingNextPage ? (
          <ActivityIndicator style={styles.footerSpinner} color={color.leaf} />
        ) : null
      }
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
  footerSpinner: { marginVertical: space.lg },
});
