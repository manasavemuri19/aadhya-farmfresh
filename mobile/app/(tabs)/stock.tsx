import { useState } from 'react';
import { FlatList, Pressable, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { adminApi } from '../../src/api/endpoints';
import { formatPaise } from '../../src/lib/money';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { AdminProduct, AdminVariant } from '../../src/api/types';

/**
 * Staff-only stock and price editor — this tab only appears in the bar for
 * role === 'staff' | 'admin' (see (tabs)/_layout.tsx). The backend
 * independently enforces the same check on every endpoint here, so this
 * screen being reachable is never itself the security boundary.
 */
export default function StockAdminScreen() {
  const insets = useSafeAreaInsets();
  const queryClient = useQueryClient();
  const products = useQuery({ queryKey: ['admin-products'], queryFn: () => adminApi.listProducts() });
  const [drafts, setDrafts] = useState<Record<string, { price?: string; stock?: string }>>({});
  const [savedSku, setSavedSku] = useState<string | null>(null);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['admin-products'] });

  const save = useMutation({
    mutationFn: async (variant: AdminVariant) => {
      const draft = drafts[variant.sku];
      if (draft?.price) {
        const rupees = parseFloat(draft.price);
        if (!Number.isNaN(rupees) && rupees >= 0) {
          await adminApi.setPrice(variant.sku, Math.round(rupees * 100));
        }
      }
      if (draft?.stock) {
        const qty = parseInt(draft.stock, 10);
        if (!Number.isNaN(qty) && qty >= 0) {
          await adminApi.setStock(variant.sku, qty);
        }
      }
    },
    onSuccess: (_, variant) => {
      setDrafts((d) => ({ ...d, [variant.sku]: {} }));
      setSavedSku(variant.sku);
      void invalidate();
      setTimeout(() => setSavedSku((s) => (s === variant.sku ? null : s)), 2000);
    },
  });

  const toggleAvailable = useMutation({
    mutationFn: (variant: AdminVariant) => adminApi.setAvailable(variant.sku, !variant.is_active),
    onSuccess: () => void invalidate(),
  });

  if (products.isPending) return <Loading label="Loading products" />;
  if (products.isError) {
    return <ErrorState message="Could not load products." onRetry={() => void products.refetch()} />;
  }

  const rows = products.data.flatMap((p: AdminProduct) =>
    p.variants.map((v: AdminVariant) => ({ ...v, productName: p.name })),
  );

  return (
    <FlatList
      style={styles.screen}
      data={rows}
      keyExtractor={(v) => v.sku}
      contentContainerStyle={[styles.list, { paddingTop: insets.top + space.md }]}
      renderItem={({ item }) => {
        const draft = drafts[item.sku] ?? {};
        const dirty = Boolean(draft.price || draft.stock);
        return (
          <View style={[styles.row, !item.is_active && styles.rowInactive]}>
            <View style={styles.rowHead}>
              <Text variant="label" numberOfLines={1} style={styles.name}>
                {item.productName} — {item.label}
              </Text>
              <Pressable onPress={() => toggleAvailable.mutate(item)} hitSlop={8}>
                <Text style={[styles.toggle, item.is_active ? styles.toggleOn : styles.toggleOff]}>
                  {item.is_active ? 'Available' : 'Sold out'}
                </Text>
              </Pressable>
            </View>

            <View style={styles.fields}>
              <View style={styles.field}>
                <Text variant="caption">Price (₹)</Text>
                <TextInput
                  style={styles.input}
                  keyboardType="decimal-pad"
                  placeholder={String(item.price_paise / 100)}
                  value={draft.price ?? ''}
                  onChangeText={(t) => setDrafts((d) => ({ ...d, [item.sku]: { ...d[item.sku], price: t } }))}
                />
              </View>
              <View style={styles.field}>
                <Text variant="caption">Stock</Text>
                <TextInput
                  style={styles.input}
                  keyboardType="number-pad"
                  placeholder={String(item.stock_qty)}
                  value={draft.stock ?? ''}
                  onChangeText={(t) => setDrafts((d) => ({ ...d, [item.sku]: { ...d[item.sku], stock: t } }))}
                />
              </View>
              <Button
                label={savedSku === item.sku ? 'Saved' : 'Save'}
                variant={dirty ? 'primary' : 'secondary'}
                disabled={!dirty}
                loading={save.isPending && save.variables?.sku === item.sku}
                onPress={() => save.mutate(item)}
                style={styles.saveButton}
              />
            </View>
            <Text variant="caption">
              currently {formatPaise(item.price_paise)} · {item.stock_qty} in stock
            </Text>
          </View>
        );
      }}
    />
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  list: { padding: space.lg, gap: space.sm },
  row: {
    backgroundColor: color.card, borderRadius: radius.md, padding: space.md,
    gap: space.xs, marginBottom: space.sm,
  },
  rowInactive: { opacity: 0.6 },
  rowHead: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: space.sm },
  name: { flex: 1 },
  toggle: { fontFamily: font.bodyBold, fontSize: size.xs },
  toggleOn: { color: color.leaf },
  toggleOff: { color: color.discount },
  fields: { flexDirection: 'row', alignItems: 'flex-end', gap: space.sm },
  field: { flex: 1, gap: 2 },
  input: {
    borderWidth: 1, borderColor: color.line, borderRadius: radius.sm,
    paddingHorizontal: space.sm, paddingVertical: 6,
    fontFamily: font.mono, fontSize: size.sm, color: color.ink,
  },
  saveButton: { minHeight: 38, paddingHorizontal: space.md },
});
