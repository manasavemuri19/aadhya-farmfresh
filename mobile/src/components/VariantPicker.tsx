import { Pressable, ScrollView, StyleSheet } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';
import type { VariantView } from '../api/types';

interface Props {
  variants: VariantView[];
  selectedSku: string;
  onSelect: (sku: string) => void;
}

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
            accessibilityLabel={unavailable ? `${variant.label}, sold out` : variant.label}
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
    height: 40,
    justifyContent: 'center',
    borderRadius: radius.sm,
    borderWidth: 1,
    borderColor: color.line,
    backgroundColor: color.card,
  },
  chipSelected: { borderColor: color.primary, backgroundColor: color.primary },
  chipUnavailable: { backgroundColor: color.surfaceAlt, borderStyle: 'dashed' },
  label: { fontFamily: font.mono, fontSize: size.sm, color: color.body },
  labelSelected: { color: color.onPrimary, fontFamily: font.monoBold },
  labelUnavailable: { color: color.muted, textDecorationLine: 'line-through' },
});
