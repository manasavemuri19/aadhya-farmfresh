import { useMemo, useState } from 'react';
import { Image, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { Button } from './Button';
import { QtyStepper } from './QtyStepper';
import { VariantPicker } from './VariantPicker';
import { color, font, radius, shadow, size, space } from '../theme/tokens';
import { formatPaise } from '../lib/money';
import { useCart } from '../store/cart';
import type { ProductView } from '../api/types';

export function ProductCard({ product }: { product: ProductView }) {
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

  return (
    <View style={styles.card}>
      <View style={styles.imageWrap}>
        {product.image_url ? (
          <Image source={{ uri: product.image_url }} style={styles.image} accessibilityIgnoresInvertColors />
        ) : (
          <View style={[styles.image, styles.imageFallback]} />
        )}
        {variant.discount_percent > 0 && (
          <View style={styles.discount}>
            <Text style={styles.discountText}>{variant.discount_percent}% off</Text>
          </View>
        )}
      </View>

      <View style={styles.body}>
        <Text variant="title" numberOfLines={2} style={styles.name}>{product.name}</Text>
        <Text variant="caption" numberOfLines={2} style={styles.description}>{product.description}</Text>
        <Text variant="caption" style={styles.eta}>{product.prep_minutes} min delivery</Text>

        <VariantPicker variants={product.variants} selectedSku={variant.sku} onSelect={setSelectedSku} />

        {variant.low_stock && variant.in_stock && (
          <Text style={styles.lowStock}>Only {variant.max_qty} left today</Text>
        )}

        <View style={styles.footer}>
          <View style={styles.priceBlock}>
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
    </View>
  );
}

const styles = StyleSheet.create({
  card: {
    backgroundColor: color.card,
    borderRadius: radius.lg,
    overflow: 'hidden',
    marginBottom: space.lg,
    ...shadow.card,
  },
  imageWrap: { position: 'relative' },
  image: { width: '100%', height: 150, backgroundColor: color.surfaceAlt },
  imageFallback: { backgroundColor: color.leafSoft },
  discount: {
    position: 'absolute',
    top: space.sm,
    left: space.sm,
    backgroundColor: color.discount,
    paddingHorizontal: space.sm,
    paddingVertical: 3,
    borderRadius: radius.sm,
  },
  discountText: { fontFamily: font.bodyBold, fontSize: size.xs, color: color.white },
  body: { padding: space.md, gap: space.xs },
  name: { fontSize: size.md },
  description: { minHeight: 30 },
  eta: { color: color.leaf, fontFamily: font.bodyMedium },
  lowStock: { fontFamily: font.bodyMedium, fontSize: size.xs, color: color.lowStock },
  footer: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginTop: space.xs,
    gap: space.sm,
  },
  priceBlock: { flexDirection: 'row', alignItems: 'baseline', gap: space.xs, flexShrink: 1 },
  mrp: { fontFamily: font.mono, fontSize: size.sm, color: color.muted, textDecorationLine: 'line-through' },
  addButton: { minHeight: 38, paddingHorizontal: space.xl },
  soldOut: { paddingHorizontal: space.md, paddingVertical: 9, borderRadius: radius.sm, backgroundColor: color.surfaceAlt },
  soldOutText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.muted },
});
