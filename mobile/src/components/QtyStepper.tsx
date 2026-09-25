import { memo } from 'react';
import { Pressable, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size } from '../theme/tokens';

interface Props {
  qty: number;
  max: number;
  onChange: (qty: number) => void;
  compact?: boolean;
}

// AAD-MOB-014: rendered inside ProductCard, which is now itself memoized —
// this only pays off if QtyStepper doesn't independently force a re-render
// whenever its parent card re-renders for an unrelated reason.
export const QtyStepper = memo(function QtyStepper({ qty, max, onChange, compact = false }: Props) {
  const atMax = qty >= max;
  return (
    <View style={[styles.wrap, compact && styles.wrapCompact]}>
      <Pressable
        onPress={() => onChange(qty - 1)}
        accessibilityRole="button"
        accessibilityLabel={qty === 1 ? 'Remove from cart' : 'Reduce quantity'}
        hitSlop={8}
        style={styles.step}
      >
        <Text style={styles.symbol}>−</Text>
      </Pressable>
      <Text style={styles.qty} accessibilityLabel={`Quantity ${qty}`}>{qty}</Text>
      <Pressable
        onPress={() => onChange(qty + 1)}
        disabled={atMax}
        accessibilityRole="button"
        accessibilityLabel="Increase quantity"
        accessibilityState={{ disabled: atMax }}
        hitSlop={8}
        style={styles.step}
      >
        <Text style={[styles.symbol, atMax && styles.symbolDisabled]}>+</Text>
      </Pressable>
    </View>
  );
});

const styles = StyleSheet.create({
  wrap: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: color.primary,
    borderRadius: radius.sm,
    height: 38,
    minWidth: 100,
    justifyContent: 'space-between',
  },
  wrapCompact: { height: 34, minWidth: 92 },
  step: { paddingHorizontal: 12, paddingVertical: 4 },
  symbol: { fontFamily: font.bodyBold, fontSize: size.md, color: color.onPrimary },
  symbolDisabled: { opacity: 0.4 },
  qty: { fontFamily: font.monoBold, fontSize: size.base, color: color.onPrimary },
});
