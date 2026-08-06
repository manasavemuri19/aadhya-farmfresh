import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { Button } from './Button';
import { color, space } from '../theme/tokens';

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <View style={styles.center}>
      <ActivityIndicator color={color.primary} />
      <Text variant="caption" style={styles.gap}>{label}</Text>
    </View>
  );
}

export function ErrorState({
  title = 'That did not load', message, onRetry,
}: { title?: string; message: string; onRetry?: () => void }) {
  return (
    <View style={styles.center}>
      <Text variant="title">{title}</Text>
      <Text variant="body" style={[styles.gap, styles.centered]}>{message}</Text>
      {onRetry && <Button label="Try again" variant="secondary" onPress={onRetry} style={styles.button} />}
    </View>
  );
}

export function EmptyState({
  title, message, actionLabel, onAction,
}: { title: string; message: string; actionLabel?: string; onAction?: () => void }) {
  return (
    <View style={styles.center}>
      <Text variant="title">{title}</Text>
      <Text variant="body" style={[styles.gap, styles.centered]}>{message}</Text>
      {actionLabel && onAction && <Button label={actionLabel} onPress={onAction} style={styles.button} />}
    </View>
  );
}

const styles = StyleSheet.create({
  center: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.xl, minHeight: 240 },
  gap: { marginTop: space.sm },
  centered: { textAlign: 'center' },
  button: { marginTop: space.lg, minWidth: 180 },
});
