import { useEffect, useState } from 'react';
import { BackHandler, Pressable, View, StyleSheet } from 'react-native';

import { Text } from './Text';
import { Button } from './Button';
import { deliveryApi } from '../api/endpoints';
import { color, font, radius, size, space } from '../theme/tokens';

const KEYPAD_ROWS = [
  ['1', '2', '3'],
  ['4', '5', '6'],
  ['7', '8', '9'],
  ['', '0', '⌫'],
] as const;

interface Props {
  /** null when there's nothing to verify — also used as the "closed" state,
   *  so the caller doesn't need a separate visible flag in sync with it. */
  order: { id: string; order_number: string } | null;
  onVerified: () => void;
  onClose: () => void;
}

/**
 * AAD-SEC-027: the delivery agent's half of in-app proof-of-delivery — the
 * only way left in this app to move an order to 'delivered' (see
 * requests.tsx's NEXT_STATUS comment; updateStatus no longer accepts it).
 *
 * Self-contained, same shape as LocationPickerModal: owns its own field
 * state and the API call, and only tells the caller when a delivery was
 * actually confirmed (onVerified) or the sheet was dismissed (onClose) —
 * the caller doesn't need to track submitting/error state of its own.
 *
 * AAD-MOB-027: this used to open the OS software keyboard through a
 * `TextInput`. Removing RN's `<Modal>` (the previous fix, still correct —
 * this stays a plain overlay, not a Modal, so there's no second Android
 * window in the mix) did not stop the flicker a screen recording had
 * shown, which means it was never the Modal specifically — it's the
 * on-screen keyboard itself misbehaving on this device (confirmed
 * happening on a OnePlus Nord CE4's Gboard). Rather than keep chasing why
 * Android's IME flickers here, this sidesteps it entirely: a custom in-app
 * number pad (plain `Pressable`s) fills the same 4-digit `code` state a
 * `TextInput` used to, with no software keyboard involved at any point —
 * there's nothing left for the OS keyboard to flicker, because it never
 * opens. `BackHandler` replaces the dismiss-on-Android-back-button
 * behaviour Modal used to give for free, from back when this was one.
 *
 * Rendered as a centered card over a dimmed backdrop rather than a full
 * slide-up sheet (LocationPickerModal's style): this is a single 4-digit
 * code entered standing at someone's door, not a screen to browse.
 */
export function VerifyDeliveryModal({ order, onVerified, onClose }: Props) {
  if (!order) return null;
  // A fresh key per order remounts the sheet below instead of needing an
  // effect to reset its fields — otherwise a stale code or error from the
  // last delivery could flash before the fields catch up to the new order.
  return <VerifyDeliveryFields key={order.id} order={order} onVerified={onVerified} onClose={onClose} />;
}

function VerifyDeliveryFields({
  order, onVerified, onClose,
}: { order: { id: string; order_number: string } } & Omit<Props, 'order'>) {
  const [code, setCode] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const codeValid = /^\d{4}$/.test(code);

  // Same dismiss behaviour the Android hardware/gesture back action got for
  // free from Modal's own onRequestClose — preserved now that this isn't a
  // Modal any more. Blocked while submitting, matching the Cancel link.
  useEffect(() => {
    const sub = BackHandler.addEventListener('hardwareBackPress', () => {
      if (submitting) return true;
      onClose();
      return true;
    });
    return () => sub.remove();
  }, [submitting, onClose]);

  const verify = async () => {
    setError(null);
    setSubmitting(true);
    try {
      await deliveryApi.verifyDelivery(order.id, code);
      onVerified();
    } catch (err) {
      // The backend's own message already says how many attempts are left
      // ("That code doesn't match. 2 attempt(s) left.") or that the code
      // is expired/locked — shown as-is rather than replaced with a
      // generic string, since the attempt count is the useful part.
      setError(err instanceof Error ? err.message : 'Could not verify that code — try again.');
    } finally {
      setSubmitting(false);
    }
  };

  const pressKey = (key: string) => {
    if (submitting) return;
    setError(null);
    if (key === '⌫') {
      setCode((c) => c.slice(0, -1));
    } else if (key && code.length < 4) {
      setCode((c) => c + key);
    }
  };

  return (
    <View style={styles.backdrop}>
      <View style={styles.card}>
        <Text variant="title">Confirm delivery</Text>
        <Text variant="caption" style={styles.hint}>
          Ask the customer for the 4-digit code shown in their Aadya app for order #
          {order.order_number}, then enter it using the keypad below.
        </Text>

        <View style={styles.digitRow} accessible accessibilityLabel={`Delivery code, ${code.length} of 4 digits entered`}>
          {[0, 1, 2, 3].map((i) => (
            <View key={i} style={[styles.digitBox, i < code.length && styles.digitBoxFilled]}>
              <Text style={styles.digitText}>{code[i] ?? ''}</Text>
            </View>
          ))}
        </View>

        {error && (
          <View style={styles.errorBox}>
            <Text style={styles.errorText}>{error}</Text>
          </View>
        )}

        <View style={styles.keypad}>
          {KEYPAD_ROWS.map((row, rowIndex) => (
            <View key={rowIndex} style={styles.keypadRow}>
              {row.map((key, keyIndex) =>
                key ? (
                  <Pressable
                    key={key}
                    onPress={() => pressKey(key)}
                    disabled={submitting}
                    accessibilityRole="button"
                    accessibilityLabel={key === '⌫' ? 'Backspace' : `Digit ${key}`}
                    style={({ pressed }) => [styles.key, pressed && styles.keyPressed]}
                  >
                    <Text style={styles.keyText}>{key}</Text>
                  </Pressable>
                ) : (
                  <View key={`${rowIndex}-blank`} style={styles.key} />
                ),
              )}
            </View>
          ))}
        </View>

        <Button
          label={submitting ? 'Verifying…' : 'Verify & mark delivered'}
          disabled={!codeValid || submitting}
          loading={submitting}
          onPress={() => void verify()}
        />
        <Text style={styles.cancelLink} onPress={submitting ? undefined : onClose}>
          Cancel
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    // Was Modal's job (its own Window always covers everything). As a plain
    // View it needs to claim that same full-screen coverage itself,
    // stacked above the FlatList and tab bar behind it, and needs it
    // through an actual explicit size — position:'absolute' with all four
    // edges pinned, not `flex: 1` alone, since a bare flex View can only
    // fill space its own parent's layout hands it, and this is a Fragment
    // sibling of the FlatList rather than that FlatList's child.
    ...StyleSheet.absoluteFillObject,
    zIndex: 1000,
    elevation: 20,
    backgroundColor: 'rgba(0,0,0,0.5)',
    alignItems: 'center',
    justifyContent: 'center',
    padding: space.lg,
  },
  card: {
    width: '100%',
    maxWidth: 360,
    backgroundColor: color.card,
    borderRadius: radius.lg,
    padding: space.lg,
    gap: space.sm,
  },
  hint: { marginBottom: space.xs },
  digitRow: {
    flexDirection: 'row',
    justifyContent: 'center',
    gap: space.sm,
    marginVertical: space.xs,
  },
  digitBox: {
    width: 48,
    height: 56,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    backgroundColor: color.surface,
    alignItems: 'center',
    justifyContent: 'center',
  },
  digitBoxFilled: { borderColor: color.primary },
  digitText: { fontFamily: font.monoBold, fontSize: 28, color: color.ink },
  keypad: { gap: space.sm },
  keypadRow: { flexDirection: 'row', justifyContent: 'center', gap: space.sm },
  key: {
    width: 64,
    height: 52,
    borderRadius: radius.md,
    backgroundColor: color.surface,
    alignItems: 'center',
    justifyContent: 'center',
  },
  keyPressed: { backgroundColor: color.line },
  keyText: { fontFamily: font.monoBold, fontSize: size.lg, color: color.ink },
  errorBox: {
    backgroundColor: color.discountSoft,
    borderRadius: radius.md,
    padding: space.md,
  },
  errorText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  cancelLink: { textAlign: 'center', color: color.muted, marginTop: 4 },
});
