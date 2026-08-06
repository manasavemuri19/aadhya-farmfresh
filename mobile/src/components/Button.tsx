import { ActivityIndicator, Pressable, StyleSheet, View, type ViewStyle } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';

interface Props {
  label: string;
  onPress: () => void;
  variant?: 'primary' | 'secondary' | 'ghost';
  disabled?: boolean;
  loading?: boolean;
  style?: ViewStyle;
  accessibilityHint?: string;
}

export function Button({
  label, onPress, variant = 'primary', disabled = false, loading = false, style,
  accessibilityHint,
}: Props) {
  const inactive = disabled || loading;
  return (
    <Pressable
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityHint={accessibilityHint}
      accessibilityState={{ disabled: inactive, busy: loading }}
      style={({ pressed }) => [
        styles.base,
        styles[variant],
        pressed && !inactive ? styles.pressed : null,
        inactive ? styles.disabled : null,
        style,
      ]}
    >
      {loading ? (
        <ActivityIndicator color={variant === 'primary' ? color.onPrimary : color.primary} />
      ) : (
        <View>
          <Text style={[styles.label, variant === 'primary' ? styles.labelOnPrimary : null]}>
            {label}
          </Text>
        </View>
      )}
    </Pressable>
  );
}

const styles = StyleSheet.create({
  base: {
    minHeight: 52,
    borderRadius: radius.md,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.lg,
  },
  primary: { backgroundColor: color.primary },
  secondary: { backgroundColor: color.card, borderWidth: 1.5, borderColor: color.primary },
  ghost: { backgroundColor: 'transparent' },
  pressed: { opacity: 0.85 },
  disabled: { opacity: 0.4 },
  label: { fontFamily: font.bodyBold, fontSize: size.base, color: color.primary },
  labelOnPrimary: { color: color.onPrimary },
});
