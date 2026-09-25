import { useState } from 'react';
import { Modal, TextInput, View, StyleSheet } from 'react-native';

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
    <Modal visible transparent animationType="fade" onRequestClose={onClose}>
      <View style={styles.backdrop}>
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
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  backdrop: {
    flex: 1,
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
