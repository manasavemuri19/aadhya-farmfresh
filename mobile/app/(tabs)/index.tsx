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
          <View style={styles.headerWrap}>
            <View style={[styles.orangeZone, { paddingTop: insets.top + space.md }]}>
              {/* Diagonal gradients need a real native library (a rebuild,
                  not an update) — this approximates the same warm orange
                  fade top-to-bottom using nothing but plain Views, so it's
                  safe to ship instantly and never risks the native-module
                  crash a half-linked gradient library caused before. Scoped
                  to just this zone, not the search bar or category rail
                  below it. */}
              <GradientBackdrop />
              <Text variant="display" style={styles.wordmark}>Aadya Dairy</Text>
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
            </View>

            <View style={styles.plainZone}>
              <View style={styles.searchWrap}>
                <SearchBar value={search} onChangeText={setSearch} />
              </View>

              <CategoryRail categories={catalog.data.categories} selected={category} onSelect={setCategory} />
            </View>
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
  headerWrap: {
    marginHorizontal: -space.lg,
  },
  orangeZone: {
    position: 'relative',
    overflow: 'hidden',
    paddingHorizontal: space.lg,
    paddingBottom: space.md,
  },
  plainZone: {
    backgroundColor: color.surface,
    paddingHorizontal: space.lg,
    paddingBottom: space.xs,
  },
  wordmark: { fontSize: size.xxl },
  tagline: { fontFamily: font.bodyMedium, marginTop: 2 },
  searchWrap: { marginTop: space.md },
  empty: { textAlign: 'center', marginTop: space.xxl },
});

/**
 * A soft top-to-bottom fade from `color.primary` into `color.primaryPressed`,
 * built from plain stacked Views — deliberately not expo-linear-gradient.
 * That package needs its native module compiled into the app, which only
 * happens on a full `eas build`; shipping code that references it through
 * `eas update` alone crashes the app on launch, since the JS bundle updates
 * instantly but the native binary it's running inside does not. Capped at
 * 80% opacity at its darkest (not fully opaque primaryPressed) so the fade
 * stays gentle rather than ending in a hard, saturated block of colour.
 */
function GradientBackdrop() {
  const BANDS = 14;
  const MAX_OPACITY = 0.8;
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      <View style={[StyleSheet.absoluteFill, { backgroundColor: color.primary }]} />
      {Array.from({ length: BANDS }).map((_, i) => (
        <View
          key={i}
          style={{
            position: 'absolute',
            left: 0,
            right: 0,
            top: `${(i / BANDS) * 100}%`,
            height: `${100 / BANDS + 1}%`, // +1 avoids hairline seams between bands
            backgroundColor: color.primaryPressed,
            opacity: ((i + 1) / BANDS) * MAX_OPACITY,
          }}
        />
      ))}
    </View>
  );
}
