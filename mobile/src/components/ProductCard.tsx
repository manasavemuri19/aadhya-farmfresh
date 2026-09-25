import { memo, useMemo, useState } from 'react';
import { Image, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { Button } from './Button';
import { QtyStepper } from './QtyStepper';
import { VariantPicker } from './VariantPicker';
import { color, font, radius, shadow, size, space } from '../theme/tokens';
import { formatPaise } from '../lib/money';
import { productImageSource } from '../lib/productImages';
import { useCart } from '../store/cart';
import type { ProductView } from '../api/types';

/**
 * Compact grid card — two per row. Name, one line of pack-size chips, price,
 * and the add control. No description or delivery-time text: at this size
 * that copy competed with the things people actually scan for (what is it,
 * what size, how much), so it's gone from the card entirely.
 */
// AAD-MOB-014: this is the cell renderer for the shop grid's FlatList — with
// no memoization, every visible card re-rendered on every keystroke in
// search (OrderTab's inline renderItem gave each one a fresh element every
// time regardless), every category tap, and every cart mutation elsewhere
// on the screen. `product` is a stable reference from the catalog query
// (filtering builds a new array, not new item objects), so a plain
// reference-equality memo is enough — no custom comparator needed.
export const ProductCard = memo(function ProductCard({ product }: { product: ProductView }) {
  const items = useCart((s) => s.items);
  const add = useCart((s) => s.add);
  const setQty = useCart((s) => s.setQty);

  const defaultSku = useMemo(
    () => product.variants.find((v) => v.in_stock)?.sku ?? product.variants[0]?.sku ?? '',
    [product.variants],
  );
  const [selectedSku, setSelectedSku] = useState(defaultSku);

  const variant = product.variants.find((v) => v.sku === selectedSku) ?? product.variants[0];
  if (!variant) return null;

  const inCart = items[variant.sku]?.qty ?? 0;

  // AAD-MOB-023: this is the primary interactive tile on the shop screen,
  // and until now carried no combined description at all — a screen reader
  // walked its individual, unlabeled Text nodes one at a time (name, then
  // each variant chip, then the price, then whatever stock text happened to
  // be present), never announcing "what is this card" as one thing.
  // Attached to the product name — the first element actually reached, and
  // the one most naturally read first — rather than restructuring the card
  // into one opaque accessible group, which would swallow VariantPicker's
  // own already-correct per-chip radio roles and the Add/QtyStepper
  // control beneath it. The visible name text is unchanged; only what's
  // spoken for it changes. Downstream price/stock text is still reachable
  // on its own right after, so this trades a little repetition for getting
  // the summary announced at all, rather than leaving it silent.
  const availabilityLabel = !variant.in_stock
    ? 'sold out'
    : variant.low_stock
      ? `only ${variant.max_qty} left`
      : 'in stock';
  const priceLabel = variant.mrp_paise
    ? `${formatPaise(variant.price_paise)}, reduced from ${formatPaise(variant.mrp_paise)}`
    : formatPaise(variant.price_paise);
  const cardSummaryLabel = `${product.name}, ${variant.label}, ${priceLabel}, ${availabilityLabel}`;

  return (
    <View style={styles.card}>
      <View style={styles.imageWrap}>
        <Image
          source={productImageSource(product.name, product.category, product.image_url)}
          style={styles.image}
          resizeMode="cover"
          accessibilityIgnoresInvertColors
        />
        {variant.discount_percent > 0 && (
          <View style={styles.discount}>
            <Text style={styles.discountText}>{variant.discount_percent}%</Text>
          </View>
        )}
      </View>

      <View style={styles.body}>
        <Text
          variant="title"
          numberOfLines={1}
          style={styles.name}
          accessibilityLabel={cardSummaryLabel}
        >
          {product.name}
        </Text>

        <VariantPicker variants={product.variants} selectedSku={variant.sku} onSelect={setSelectedSku} />

        {variant.low_stock && variant.in_stock && (
          <Text style={styles.lowStock}>Only {variant.max_qty} left</Text>
        )}

        <View style={styles.priceRow}>
          <Text variant="price">{formatPaise(variant.price_paise)}</Text>
          {variant.mrp_paise ? <Text style={styles.mrp}>{formatPaise(variant.mrp_paise)}</Text> : null}
        </View>

        {!variant.in_stock ? (
          <View style={styles.soldOut}>
            <Text style={styles.soldOutText}>Sold out</Text>
          </View>
        ) : inCart > 0 ? (
          <QtyStepper
            qty={inCart}
            max={variant.max_qty}
            onChange={(qty) => setQty(variant.sku, qty, variant.max_qty)}
            compact
          />
        ) : (
          <Button
            label="Add"
            variant="secondary"
            style={styles.addButton}
            accessibilityHint={`Adds ${product.name}, ${variant.label}, to your cart`}
            onPress={() =>
              add(
                {
                  sku: variant.sku,
                  productName: product.name,
                  variantLabel: variant.label,
                  imageUrl: product.image_url,
                },
                variant.max_qty,
              )
            }
          />
        )}
      </View>
    </View>
  );
});

const styles = StyleSheet.create({
  card: {
    backgroundColor: color.card,
    borderRadius: radius.lg,
    overflow: 'hidden',
    ...shadow.card,
  },
  imageWrap: { position: 'relative' },
  // A fixed height, not aspectRatio, is deliberate: aspectRatio computed
  // against a percentage width inside a nested flex grid is a known flaky
  // spot on Android's renderer — it can measure before the column width is
  // settled and lock in a wrong (often much taller) result. A fixed height
  // sidesteps that entirely and is also what gives quick-commerce grids
  // (Zepto, Blinkit) their uniform tile look regardless of column width.
  image: { width: '100%', height: 118, backgroundColor: color.surfaceAlt },
  discount: {
    position: 'absolute',
    top: space.xs,
    left: space.xs,
    backgroundColor: color.discount,
    paddingHorizontal: 6,
    paddingVertical: 2,
    borderRadius: radius.sm,
  },
  discountText: { fontFamily: font.bodyBold, fontSize: 10, color: color.white },
  body: { padding: space.sm, gap: 6 },
  name: { fontSize: size.base },
  lowStock: { fontFamily: font.bodyMedium, fontSize: 10, color: color.lowStock },
  priceRow: { flexDirection: 'row', alignItems: 'baseline', gap: 6, flexWrap: 'wrap' },
  mrp: { fontFamily: font.mono, fontSize: 11, color: color.muted, textDecorationLine: 'line-through' },
  addButton: { minHeight: 34 },
  soldOut: {
    paddingVertical: 8, borderRadius: radius.sm, backgroundColor: color.surfaceAlt,
    alignItems: 'center',
  },
  soldOutText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.muted },
});
