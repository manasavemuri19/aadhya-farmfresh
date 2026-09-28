import { useEffect, useState } from 'react';
import { BackHandler, KeyboardAvoidingView, Platform, TextInput, View, StyleSheet } from 'react-native';

import { Text } from './Text';
import { Button } from './Button';
import { deliveryApi } from '../api/endpoints';
import { color, font, radius, size, space } from '../theme/tokens';

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
 * AAD-MOB-027: this used to be RN's own <Modal>. On Android, Modal opens a
 * *second* native Window stacked on top of the Activity's — and that
 * second window fighting the main one for IME (keyboard) ownership is a
 * long-documented RN/Android interaction bug: the keyboard shows, Android
 * decides the other window should have it, hides it, this TextInput
 * re-requests focus, it shows again — a rapid open/close loop, worse on
 * some OEM keyboards (the OnePlus Nord CE4's Gboard build included) than
 * stock Android. That's exactly what the screen recording showed: the
 * keyboard flickering in and out on its own, nothing ever actually typed.
 * There was nothing to debounce or pause here — it isn't a re-render
 * problem, it's this specific Android + Modal combination. Rendered as a
 * plain absolutely-positioned overlay instead, it lives in the same single
 * window as the rest of the screen, so there's no second window to fight
 * the keyboard over. `BackHandler` below replaces the dismiss-on-Android-
 * back-button behaviour Modal used to give for free.
 *
 * Rendered as a centered card over a dimmed backdrop rather than a full
 * slide-up sheet (LocationPickerModal's style): this is a single 4-digit
 * field entered standing at someone's door, not a screen to browse.
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

  return (
    <View style={styles.backdrop}>
      <KeyboardAvoidingView
        style={styles.avoider}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.card}>
          <Text variant="title">Confirm delivery</Text>
          <Text variant="caption" style={styles.hint}>
            Ask the customer for the 4-digit code shown in their Aadya app for order #
            {order.order_number}, then enter it below.
          </Text>

          <TextInput
            value={code}
            onChangeText={(t) => {
              setError(null);
              setCode(t.replace(/\D/g, '').slice(0, 4));
            }}
            keyboardType="number-pad"
            maxLength={4}
            placeholder="0000"
            style={styles.input}
            autoFocus
            accessibilityLabel="Delivery code"
          />

          {error && (
            <View style={styles.errorBox}>
              <Text style={styles.errorText}>{error}</Text>
            </View>
          )}

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
      </KeyboardAvoidingView>
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
  avoider: { width: '100%', alignItems: 'center' },
  card: {
    width: '100%',
    maxWidth: 360,
    backgroundColor: color.card,
    borderRadius: radius.lg,
    padding: space.lg,
    gap: space.sm,
  },
  hint: { marginBottom: space.xs },
  input: {
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 56,
    fontFamily: font.monoBold,
    fontSize: 28,
    letterSpacing: 8,
    textAlign: 'center',
    color: color.ink,
  },
  errorBox: {
    backgroundColor: color.discountSoft,
    borderRadius: radius.md,
    padding: space.md,
  },
  errorText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  cancelLink: { textAlign: 'center', color: color.muted, marginTop: 4 },
});
