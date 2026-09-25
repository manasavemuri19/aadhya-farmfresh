import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { Text } from './Text';
import { Button } from './Button';
import { describeError } from '../api/client';
import { color, space } from '../theme/tokens';

// AAD-MOB-023: neither of these announced itself to a screen reader when it
// appeared — a sighted user sees the spinner or the error message replace
// whatever was on screen; a TalkBack/VoiceOver user, focused elsewhere,
// heard nothing until they happened to swipe into it. `accessibilityLiveRegion`
// (Android) and the `accessible`/role pairing RN maps to the iOS equivalent
// make each one speak up on its own the moment it mounts, same as a sighted
// user's eye is drawn to it.
export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <View
      style={styles.center}
      accessible
      accessibilityLiveRegion="polite"
      accessibilityLabel={label}
    >
      <ActivityIndicator color={color.primary} />
      <Text variant="caption" style={styles.gap}>{label}</Text>
    </View>
  );
}

// AAD-MOB-026 (revised scope): used to take a pre-built `message` string
// (and often a per-screen `title`), so every screen that failed to load
// showed its own wording — the server's literal error text on some screens,
// a bespoke sentence on others. Now takes the query's `error` directly and
// resolves it to one of exactly two sentences via `describeError` — a
// connection problem, or a generic "something went wrong" for everything
// else — so every screen that fails to load looks and reads the same way,
// which is the whole point: there's nothing more specific for the person to
// act on regardless of which one it technically was.
export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = describeError(error);
  return (
    <View style={styles.center}>
      {/* Only the message announces itself — grouping the "Try again" button
          into the same accessible node as well would make it one opaque
          unit and cost the button its own independent focus stop, which
          matters more here than for Loading (nothing tappable there). */}
      <View
        accessible
        accessibilityLiveRegion="assertive"
        accessibilityRole="alert"
        accessibilityLabel={message}
      >
        <Text variant="body" style={styles.centered}>{message}</Text>
      </View>
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
