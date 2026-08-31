import { useEffect, useMemo, useRef, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { ErrorState, Loading } from '../src/components/Feedback';
import { cartApi, ordersApi } from '../src/api/endpoints';
import { ApiError } from '../src/api/client';
import { cartLines, useCart } from '../src/store/cart';
import { useSession } from '../src/store/session';
import { useLocationStore } from '../src/store/location';
import { formatPaise } from '../src/lib/money';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address, PaymentMethod } from '../src/api/types';

/** Stable per checkout attempt. Survives re-renders and retries, so a dropped
 *  response cannot become a second order. */
function useIdempotencyKey(): string {
  const ref = useRef<string>();
  if (!ref.current) {
    ref.current = `checkout-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  }
  return ref.current;
}

export default function CheckoutScreen() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const items = useCart((s) => s.items);
  const clearCart = useCart((s) => s.clear);
  const idempotencyKey = useIdempotencyKey();

  const [line1, setLine1] = useState('');
  const [landmark, setLandmark] = useState('');
  const [pincode, setPincode] = useState('');
  const [notes, setNotes] = useState('');
  const [method, setMethod] = useState<PaymentMethod>('online');
  const [prefilled, setPrefilled] = useState(false);
  // Tracks whichever source last filled the address text fields, so the
  // order actually carries real coordinates instead of always submitting
  // null — the two effects below are what set this, mirroring exactly
  // which fields they set alongside.
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(null);

  const user = useSession((s) => s.user);
  const location = useLocationStore();

  // Fill from a saved profile address first; only reach for device location
  // if there isn't one, and never overwrite something the person has already
  // started typing.
  useEffect(() => {
    if (prefilled) return;
    const saved = user?.addresses?.[0];
    if (saved?.line1) {
      setLine1(saved.line1);
      setLandmark(saved.landmark ?? '');
      setPincode(saved.pincode ?? '');
      if (saved.latitude != null && saved.longitude != null) {
        setCoords({ latitude: saved.latitude, longitude: saved.longitude });
      }
      setPrefilled(true);
    }
  }, [user, prefilled]);

  const useCurrentLocation = () => {
    if (location.status === 'found' && location.line1) {
      setLine1(location.line1);
      if (location.pincode) setPincode(location.pincode);
      if (location.latitude != null && location.longitude != null) {
        setCoords({ latitude: location.latitude, longitude: location.longitude });
      }
      setPrefilled(true);
    } else {
      void location.request();
    }
  };

  // If the location finishes fetching after the button was already tapped
  // once (first tap only requests permission), apply it as soon as it lands.
  useEffect(() => {
    if (location.status === 'found' && location.line1 && line1.trim().length === 0) {
      setLine1(location.line1);
      if (location.pincode) setPincode(location.pincode);
      if (location.latitude != null && location.longitude != null) {
        setCoords({ latitude: location.latitude, longitude: location.longitude });
      }
    }
  }, [location.status]);

  const lines = useMemo(() => cartLines(items), [items]);

  const quote = useQuery({
    queryKey: ['quote', lines],
    queryFn: () => cartApi.quote(lines),
    enabled: lines.length > 0,
  });

  const placeOrder = useMutation({
    mutationFn: () => {
      const address: Address = {
        label: 'Home',
        line1: line1.trim(),
        line2: '',
        landmark: landmark.trim(),
        city: 'Hyderabad',
        pincode: pincode.trim(),
        latitude: coords?.latitude ?? null,
        longitude: coords?.longitude ?? null,
      };
      return ordersApi.create(
        {
          lines,
          address,
          payment_method: method,
          notes: notes.trim(),
          // Server rejects the order if its own total differs. The customer is
          // never charged a number they did not see.
          expected_total_paise: quote.data?.total_paise,
        },
        idempotencyKey,
      );
    },
    onSuccess: (order) => {
      clearCart();
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
      void queryClient.invalidateQueries({ queryKey: ['catalog'] });
      // Cash orders are already confirmed server-side — go straight to the
      // order. Online orders sit in pending_payment until /payments/verify
      // is called, which is what the payment screen does.
      if (order.payment.method === 'online' && order.status === 'pending_payment') {
        router.replace(`/payment?orderId=${order.id}`);
      } else {
        router.replace(`/order/${order.id}`);
      }
    },
  });

  const addressValid = line1.trim().length >= 4 && /^\d{6}$/.test(pincode.trim());

  if (quote.isPending) return <Loading />;
  if (quote.isError) {
    return (
      <ErrorState
        message={quote.error instanceof Error ? quote.error.message : 'Try again.'}
        onRetry={() => void quote.refetch()}
      />
    );
  }

  const error = placeOrder.error;
  const stockProblem = error instanceof ApiError && error.code === 'out_of_stock';

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text variant="title">Where should we deliver?</Text>

        <Pressable
          onPress={useCurrentLocation}
          accessibilityRole="button"
          style={({ pressed }) => [styles.locationButton, pressed && styles.locationButtonPressed]}
        >
          <Text style={styles.locationButtonText}>
            {location.status === 'locating' ? '📍 Finding your location…' : '📍 Use my current location'}
          </Text>
        </Pressable>

        <Field
          label="Flat, building and street"
          value={line1}
          onChangeText={(t) => { setLine1(t); setCoords(null); }}
          placeholder="12-3-45, Rose Villa, Banjara Hills"
          autoComplete="street-address"
        />
        <Field
          label="Landmark (optional)"
          value={landmark}
          onChangeText={setLandmark}
          placeholder="Opposite the temple"
        />
        <Field
          label="Pincode"
          value={pincode}
          onChangeText={(t) => { setPincode(t); setCoords(null); }}
          placeholder="500034"
          keyboardType="number-pad"
          maxLength={6}
        />
        <Field
          label="Note for the rider (optional)"
          value={notes}
          onChangeText={setNotes}
          placeholder="Ring the bell twice"
          maxLength={280}
        />

        <Text variant="title" style={styles.sectionGap}>How would you like to pay?</Text>
        <PayOption
          label="Pay now"
          detail="UPI, card or netbanking"
          selected={method === 'online'}
          onPress={() => setMethod('online')}
        />
        <PayOption
          label="Pay on delivery"
          detail="Cash or UPI at the door"
          selected={method === 'cod'}
          onPress={() => setMethod('cod')}
        />

        <View style={styles.summary}>
          <View style={styles.summaryRow}>
            <Text variant="body">Items</Text>
            <Text variant="priceSmall">{formatPaise(quote.data.subtotal_paise)}</Text>
          </View>
          <View style={styles.summaryRow}>
            <Text variant="body">Delivery</Text>
            <Text variant="priceSmall">
              {quote.data.delivery_fee_paise === 0
                ? 'Free'
                : formatPaise(quote.data.delivery_fee_paise)}
            </Text>
          </View>
          <View style={styles.divider} />
          <View style={styles.summaryRow}>
            <Text variant="label" style={styles.totalLabel}>Total</Text>
            <Text style={styles.totalValue}>{formatPaise(quote.data.total_paise)}</Text>
          </View>
        </View>

        {error && (
          <View style={styles.errorBox}>
            <Text style={styles.errorText}>
              {error instanceof Error ? error.message : 'Could not place the order.'}
            </Text>
            {stockProblem && (
              <Pressable onPress={() => router.replace('/cart')}>
                <Text style={styles.errorLink}>Review your cart →</Text>
              </Pressable>
            )}
          </View>
        )}
      </ScrollView>

      <View style={styles.footer}>
        <Button
          label={method === 'cod' ? 'Place order' : `Pay ${formatPaise(quote.data.total_paise)}`}
          disabled={!addressValid}
          loading={placeOrder.isPending}
          onPress={() => placeOrder.mutate()}
        />
      </View>
    </KeyboardAvoidingView>
  );
}

function Field({
  label, ...props
}: { label: string } & React.ComponentProps<typeof TextInput>) {
  return (
    <View style={styles.field}>
      <Text variant="caption">{label}</Text>
      <TextInput
        {...props}
        style={styles.input}
        placeholderTextColor={color.muted}
        accessibilityLabel={label}
      />
    </View>
  );
}

function PayOption({
  label, detail, selected, onPress,
}: { label: string; detail: string; selected: boolean; onPress: () => void }) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="radio"
      accessibilityState={{ selected }}
      style={[styles.payOption, selected && styles.payOptionSelected]}
    >
      <View style={styles.payText}>
        <Text variant="label">{label}</Text>
        <Text variant="caption">{detail}</Text>
      </View>
      <View style={[styles.radio, selected && styles.radioSelected]} />
    </Pressable>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  sectionGap: { marginTop: space.md },
  locationButton: {
    backgroundColor: color.leafSoft,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.leaf,
    paddingVertical: space.sm,
    paddingHorizontal: space.md,
    alignItems: 'center',
  },
  locationButtonPressed: { opacity: 0.8 },
  locationButtonText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.leaf },
  field: { gap: space.xs },
  input: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 50,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
  },
  payOption: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    padding: space.md,
    minHeight: 60,
  },
  payOptionSelected: { borderColor: color.ink },
  payText: { gap: 2 },
  radio: {
    width: 20, height: 20, borderRadius: 10,
    borderWidth: 2, borderColor: color.line,
  },
  radioSelected: { borderColor: color.ink, borderWidth: 6 },
  summary: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
    gap: space.sm,
    marginTop: space.md,
  },
  summaryRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  divider: { height: 1, backgroundColor: color.line },
  totalLabel: { fontSize: size.md },
  totalValue: { fontFamily: font.monoBold, fontSize: size.lg, color: color.ink },
  errorBox: {
    backgroundColor: color.discountSoft,
    borderRadius: radius.md,
    padding: space.md,
    gap: space.xs,
  },
  errorText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  errorLink: { fontFamily: font.bodyBold, fontSize: size.sm, color: color.discount },
  footer: {
    padding: space.lg,
    borderTopWidth: 1,
    borderTopColor: color.line,
    backgroundColor: color.card,
  },
});
