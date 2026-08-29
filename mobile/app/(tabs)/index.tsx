import { useEffect, useMemo, useState } from 'react';
import { FlatList, RefreshControl, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useQuery } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { ProductCard } from '../../src/components/ProductCard';
import { CategoryRail } from '../../src/components/CategoryRail';
import { SearchBar } from '../../src/components/SearchBar';
import { CartBar } from '../../src/components/CartBar';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { catalogApi } from '../../src/api/endpoints';
import { cartCount, useCart } from '../../src/store/cart';
import { useLocationStore } from '../../src/store/location';
import { color, font, size, space } from '../../src/theme/tokens';
import type { ProductView } from '../../src/api/types';

export default function OrderTab() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [category, setCategory] = useState('all');
  const [search, setSearch] = useState('');
  const items = useCart((s) => s.items);
  const location = useLocationStore();

  useEffect(() => {
    if (location.status === 'idle') void location.request();
  }, [location]);

  const catalog = useQuery({ queryKey: ['catalog'], queryFn: () => catalogApi.get() });

  // Filter locally by category and search term — the catalog is small enough
  // that both should feel instant rather than round-tripping to the server.
  const products = useMemo<ProductView[]>(() => {
    let list = catalog.data?.products ?? [];
    if (category !== 'all') list = list.filter((p: ProductView) => p.category === category);
    const term = search.trim().toLowerCase();
    if (term) {
      list = list.filter(
        (p: ProductView) =>
          p.name.toLowerCase().includes(term) || p.description.toLowerCase().includes(term),
      );
    }
    return list;
  }, [catalog.data, category, search]);

  const count = cartCount(items);

  if (catalog.isPending) return <Loading label="Bringing in today's stock" />;
  if (catalog.isError) {
    return (
      <ErrorState
        title="Could not reach the farm"
        message={catalog.error instanceof Error ? catalog.error.message : 'Check your connection and try again.'}
        onRetry={() => void catalog.refetch()}
      />
    );
  }

  return (
    <View style={styles.screen}>
      <FlatList
        data={products}
        keyExtractor={(item) => item.id}
        renderItem={({ item }: { item: ProductView }) => (
          <View style={styles.gridItem}>
            <ProductCard product={item} />
          </View>
        )}
        numColumns={2}
        columnWrapperStyle={styles.gridRow}
        contentContainerStyle={[styles.list, { paddingBottom: count > 0 ? 110 : space.xl }]}
        showsVerticalScrollIndicator={false}
        stickyHeaderIndices={[0]}
        ListHeaderComponent={
          <View style={[styles.header, { paddingTop: insets.top + space.md }]}>
            <Text variant="display" style={styles.wordmark}>Aadya</Text>
            <Text
              variant="caption"
              style={styles.tagline}
              numberOfLines={1}
              onPress={() => {
                if (location.status === 'denied' || location.status === 'error') {
                  router.push('/profile');
                } else {
                  void location.request();
                }
              }}
            >
              {location.status === 'found' && location.label
                ? `📍 ${location.label} · 20–45 min`
                : location.status === 'locating'
                  ? 'Finding your location…'
                  : location.status === 'denied'
                    ? 'Tap to set delivery address'
                    : 'Pickles & Dairy · 20–45 min'}
            </Text>

            <View style={styles.searchWrap}>
              <SearchBar value={search} onChangeText={setSearch} />
            </View>

            <CategoryRail categories={catalog.data.categories} selected={category} onSelect={setCategory} />
          </View>
        }
        refreshControl={
          <RefreshControl
            refreshing={catalog.isRefetching}
            onRefresh={() => void catalog.refetch()}
            tintColor={color.primary}
          />
        }
        ListEmptyComponent={
          <Text variant="body" style={styles.empty}>
            {search ? `Nothing matches "${search}".` : 'Nothing in this section today. Try another.'}
          </Text>
        }
      />

      <CartBar count={count} totalPaise={null} onPress={() => router.push('/cart')} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  list: { paddingHorizontal: space.lg },
  gridRow: { gap: space.sm },
  gridItem: { flex: 1, marginBottom: space.sm },
  header: {
    backgroundColor: color.surface,
    marginHorizontal: -space.lg,
    paddingHorizontal: space.lg,
    paddingBottom: space.xs,
  },
  wordmark: { fontSize: size.xxl },
  tagline: { fontFamily: font.bodyMedium, marginTop: 2 },
  searchWrap: { marginTop: space.md },
  empty: { textAlign: 'center', marginTop: space.xxl },
});
