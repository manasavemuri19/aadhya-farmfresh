import { Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';
import { formatPaise } from '../lib/money';

interface Props {
  count: number;
  totalPaise: number | null;
  onPress: () => void;
  label?: string;
  /**
   * Extra space to leave below the bar — e.g. so it stacks above the
   * OrderTrackerBar (a persistent order-in-progress strip that lives at the
   * tabs-layout level, pinned just above the tab bar) instead of rendering
   * underneath it. Both bars are absolutely positioned bottom-anchored, so
   * without this they land in the same spot and overlap whenever a customer
   * has both an active order and something in their cart.
   */
  bottomOffset?: number;
}

export function CartBar({
  count, totalPaise, onPress, label = 'View cart', bottomOffset = 0,
}: Props) {
  const insets = useSafeAreaInsets();
  if (count === 0) return null;

  return (
    <View
      style={[
        styles.wrap,
        { bottom: bottomOffset, paddingBottom: Math.max(insets.bottom, space.md) },
      ]}
    >
      <Pressable
        onPress={onPress}
        accessibilityRole="button"
        accessibilityLabel={`${label}, ${count} ${count === 1 ? 'item' : 'items'}`}
        style={({ pressed }) => [styles.bar, pressed && styles.pressed]}
      >
        <View>
          <Text style={styles.count}>{count} {count === 1 ? 'item' : 'items'}</Text>
          {totalPaise !== null && <Text style={styles.total}>{formatPaise(totalPaise)}</Text>}
        </View>
        <Text style={styles.cta}>{label} →</Text>
      </Pressable>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    position: 'absolute',
    left: 0, right: 0,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
  },
  bar: {
    backgroundColor: color.primary,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.md,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    minHeight: 58,
  },
  pressed: { opacity: 0.9 },
  count: { fontFamily: font.body, fontSize: size.xs, color: '#F7E4D6' },
  total: { fontFamily: font.monoBold, fontSize: size.md, color: color.onPrimary },
  cta: { fontFamily: font.bodyBold, fontSize: size.base, color: color.onPrimary },
});
