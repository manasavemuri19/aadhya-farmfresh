import { Pressable, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';
import type { OrderStatus } from '../api/types';

const LABEL: Partial<Record<OrderStatus, string>> = {
  confirmed: 'Preparing your order',
  packed: 'Packed and ready',
  out_for_delivery: 'On the way',
};

interface Props {
  orderNumber: string;
  status: OrderStatus;
  onPress: () => void;
}

/**
 * A persistent "order in progress" strip, in the spirit of the tracker bars
 * apps like Zepto keep pinned above their tab bar while something is out
 * for delivery. Static for now — a live ETA/timer can layer on top of this
 * later without changing where it lives or how it's triggered.
 */
export function OrderTrackerBar({ orderNumber, status, onPress }: Props) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      accessibilityLabel={`Order ${orderNumber}, ${LABEL[status] ?? 'in progress'}. Tap to track.`}
      style={({ pressed }) => [styles.bar, pressed && styles.pressed]}
    >
      <View style={styles.dot} />
      <View style={styles.body}>
        <Text style={styles.title} numberOfLines={1}>{LABEL[status] ?? 'Order in progress'}</Text>
        <Text style={styles.subtitle} numberOfLines={1}>Order {orderNumber}</Text>
      </View>
      <Text style={styles.cta}>Track →</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  bar: {
    backgroundColor: color.leaf,
    borderRadius: radius.md,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    minHeight: 52,
  },
  pressed: { opacity: 0.9 },
  dot: { width: 8, height: 8, borderRadius: 4, backgroundColor: color.onPrimary },
  body: { flex: 1, gap: 1 },
  title: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.onPrimary },
  subtitle: { fontFamily: font.body, fontSize: size.xs, color: color.leafSoft },
  cta: { fontFamily: font.bodyBold, fontSize: size.sm, color: color.onPrimary },
});
