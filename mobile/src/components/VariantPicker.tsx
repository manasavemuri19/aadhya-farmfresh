import { Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';
import type { VariantView } from '../api/types';

interface Props {
  variants: VariantView[];
  selectedSku: string;
  onSelect: (sku: string) => void;
}

/**
 * The size selector.
 *
 * This is the app's signature control, and it earns the attention: a dairy is
 * a shop where the *quantity* is the product decision — 500 ml or 5 litres is
 * a different purchase, not a different option. So the sizes are laid out as a
 * row of weights, mono-set, sold-out ones struck through rather than hidden,
 * because a customer looking for the 5 litre can should see that it exists and
 * has run out today.
 */
export function VariantPicker({ variants, selectedSku, onSelect }: Props) {
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={styles.row}
      accessibilityRole="radiogroup"
    >
      {variants.map((variant) => {
        const selected = variant.sku === selectedSku;
        const unavailable = !variant.in_stock;
        return (
          <Pressable
            key={variant.sku}
            onPress={() => onSelect(variant.sku)}
            disabled={unavailable}
            accessibilityRole="radio"
            accessibilityState={{ selected, disabled: unavailable }}
            accessibilityLabel={
              unavailable ? `${variant.label}, sold out` : variant.label
            }
            style={[
              styles.chip,
              selected && styles.chipSelected,
              unavailable && styles.chipUnavailable,
            ]}
          >
            <Text
              style={[
                styles.label,
                selected && styles.labelSelected,
                unavailable && styles.labelUnavailable,
              ]}
            >
              {variant.label}
            </Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  row: { gap: space.sm, paddingVertical: space.xs },
  chip: {
    paddingHorizontal: space.md,
    paddingVertical: 7,
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: color.line,
    backgroundColor: color.card,
  },
  chipSelected: { borderColor: color.ink, backgroundColor: color.ink },
  chipUnavailable: { backgroundColor: color.surface, borderStyle: 'dashed' },
  label: { fontFamily: font.mono, fontSize: size.sm, color: color.body },
  labelSelected: { color: color.white, fontFamily: font.monoBold },
  labelUnavailable: { color: color.muted, textDecorationLine: 'line-through' },
});
