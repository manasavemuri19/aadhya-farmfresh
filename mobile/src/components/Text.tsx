import { Text as RNText, StyleSheet, type TextProps } from 'react-native';
import { color, font, size } from '../theme/tokens';

type Variant = 'display' | 'title' | 'body' | 'label' | 'caption' | 'price' | 'priceSmall';

interface Props extends TextProps {
  variant?: Variant;
  tone?: keyof typeof color;
}

export function Text({ variant = 'body', tone, style, ...rest }: Props) {
  return (
    <RNText
      {...rest}
      style={[styles[variant], tone ? { color: color[tone] } : null, style]}
    />
  );
}

const styles = StyleSheet.create({
  display: { fontFamily: font.displayBold, fontSize: size.xxl, color: color.ink, letterSpacing: -0.6 },
  title: { fontFamily: font.display, fontSize: size.lg, color: color.ink, letterSpacing: -0.3 },
  body: { fontFamily: font.body, fontSize: size.base, color: color.body, lineHeight: 22 },
  label: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.ink },
  caption: { fontFamily: font.body, fontSize: size.xs, color: color.muted },
  price: { fontFamily: font.monoBold, fontSize: size.md, color: color.primary },
  priceSmall: { fontFamily: font.mono, fontSize: size.sm, color: color.muted },
});
