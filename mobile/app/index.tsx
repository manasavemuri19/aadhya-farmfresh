import { useMemo, useState } from 'react';
import { FlatList, Pressable, RefreshControl, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useQuery } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { ProductCard } from '../src/components/ProductCard';
import { CategoryRail } from '../src/components/CategoryRail';
import { SearchBar } from '../src/components/SearchBar';
import { CartBar } from '../src/components/CartBar';
import { DrawerMenu } from '../src/components/DrawerMenu';
import { ErrorState, Loading } from '../src/components/Feedback';
import { catalogApi } from '../src/api/endpoints';
import { cartCount, useCart } from '../src/store/cart';
import { color, font, size, space } from '../src/theme/tokens';
import type { ProductView } from '../src/api/types';

export default function ShopScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const [category, setCategory] = useState('all');
  const [search, setSearch] = useState('');
  const [menuOpen, setMenuOpen] = useState(false);
  const items = useCart((s) => s.items);

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
        renderItem={({ item }: { item: ProductView }) => <ProductCard product={item} />}
        contentContainerStyle={[styles.list, { paddingBottom: count > 0 ? 110 : space.xl }]}
        showsVerticalScrollIndicator={false}
        stickyHeaderIndices={[0]}
        ListHeaderComponent={
          <View style={[styles.header, { paddingTop: insets.top + space.md }]}>
            <View style={styles.titleRow}>
              <View style={styles.titleBlock}>
                <Text variant="display" style={styles.wordmark}>Aadhya</Text>
                <Text variant="caption" style={styles.tagline}>Pickles &amp; Dairy · 20–45 min</Text>
              </View>
              <Pressable
                onPress={() => setMenuOpen(true)}
                accessibilityRole="button"
                accessibilityLabel="Open menu"
                hitSlop={10}
                style={styles.menuButton}
              >
                <Text style={styles.menuDots}>⋯</Text>
              </Pressable>
            </View>

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
      <DrawerMenu visible={menuOpen} onClose={() => setMenuOpen(false)} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  list: { paddingHorizontal: space.lg },
  header: {
    backgroundColor: color.surface,
    marginHorizontal: -space.lg,
    paddingHorizontal: space.lg,
    paddingBottom: space.xs,
  },
  titleRow: { flexDirection: 'row', alignItems: 'flex-start', justifyContent: 'space-between' },
  titleBlock: { flex: 1 },
  wordmark: { fontSize: size.xxl },
  tagline: { fontFamily: font.bodyMedium, marginTop: 2 },
  menuButton: {
    width: 44, height: 44, borderRadius: 22,
    alignItems: 'center', justifyContent: 'center',
    backgroundColor: color.card, borderWidth: 1, borderColor: color.line,
    marginTop: space.xs,
  },
  menuDots: { fontFamily: font.bodyBold, fontSize: 24, color: color.ink, marginTop: -6 },
  searchWrap: { marginTop: space.md },
  empty: { textAlign: 'center', marginTop: space.xxl },
});
