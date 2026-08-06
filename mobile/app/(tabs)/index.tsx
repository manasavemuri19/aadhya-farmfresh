import { useMemo, useState } from 'react';
import { FlatList, RefreshControl, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useQuery } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { ProductCard } from '../../src/components/ProductCard';
import { CategoryRail } from '../../src/components/CategoryRail';
import { CartBar } from '../../src/components/CartBar';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { catalogApi } from '../../src/api/endpoints';
import { cartCount, useCart } from '../../src/store/cart';
import { color, font, size, space } from '../../src/theme/tokens';
import type { ProductView } from '../../src/api/types';

export default function ShopScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [category, setCategory] = useState('all');
  const items = useCart((s) => s.items);

  const catalog = useQuery({
    queryKey: ['catalog'],
    queryFn: () => catalogApi.get(),
  });

  // Filter client-side: the whole catalog is a few dozen items, so switching a
  // category should be instant rather than a network round trip.
  const products = useMemo<ProductView[]>(() => {
    const all = catalog.data?.products ?? [];
    return category === 'all' ? all : all.filter((p: ProductView) => p.category === category);
  }, [catalog.data, category]);

  const count = cartCount(items);

  if (catalog.isPending) return <Loading label="Bringing in today's stock" />;

  if (catalog.isError) {
    return (
      <ErrorState
        title="Could not reach the farm"
        message={
          catalog.error instanceof Error
            ? catalog.error.message
            : 'Check your connection and try again.'
        }
        onRetry={() => void catalog.refetch()}
      />
    );
  }

  return (
    <View style={styles.screen}>
      <View style={[styles.header, { paddingTop: insets.top + space.md }]}>
        <Text variant="display" style={styles.wordmark}>Aadhya</Text>
        <Text variant="caption" style={styles.tagline}>
          Pickles &amp; Dairy · delivered in 20–45 min
        </Text>
      </View>

      <CategoryRail
        categories={catalog.data.categories}
        selected={category}
        onSelect={setCategory}
      />

      <FlatList
        data={products}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => <ProductCard product={item} />}
        contentContainerStyle={[
          styles.list,
          { paddingBottom: count > 0 ? 110 : space.xl },
        ]}
        showsVerticalScrollIndicator={false}
        refreshControl={
          <RefreshControl
            refreshing={catalog.isRefetching}
            onRefresh={() => void catalog.refetch()}
            tintColor={color.ink}
          />
        }
        ListEmptyComponent={
          <Text variant="body" style={styles.empty}>
            Nothing in this section today. Try another.
          </Text>
        }
      />

      <CartBar count={count} totalPaise={null} onPress={() => router.push('/cart')} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  header: { paddingHorizontal: space.lg, paddingBottom: space.xs },
  wordmark: { fontSize: size.xxl },
  tagline: { fontFamily: font.bodyMedium, marginTop: 2 },
  list: { paddingHorizontal: space.lg, paddingTop: space.sm },
  empty: { textAlign: 'center', marginTop: space.xxl },
});