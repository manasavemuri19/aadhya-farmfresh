import { useEffect, useMemo, useRef, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { ErrorState, Loading } from '../src/components/Feedback';
import { LocationPickerModal, type PickedLocation } from '../src/components/LocationPickerModal';
import { cartApi, ordersApi } from '../src/api/endpoints';
import { ApiError } from '../src/api/client';
import { cartLines, useCart } from '../src/store/cart';
import { useSession } from '../src/store/session';
import { useLocationStore } from '../src/store/location';
import { formatPaise } from '../src/lib/money';
import { DEFAULT_ADDRESS_LABEL, SERVICE_CITY } from '../src/lib/address';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address, PaymentMethod } from '../src/api/types';

/** Stable per checkout attempt. Survives re-renders and retries, so a dropped
 *  response cannot become a second order. */
function useIdempotencyKey(): string {
  const ref = useRef<string | undefined>(undefined);
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
  // AAD-MOB-022 (multi-address support): which saved address (if any) the
  // form currently matches — null means "typed/detected manually, not one
  // of the saved ones". Set whenever a saved address is picked below, and
  // cleared the moment any field is hand-edited or filled from location
  // detection instead, since at that point the form no longer necessarily
  // matches what's actually saved under that label.
  const [selectedAddressLabel, setSelectedAddressLabel] = useState<string | null>(null);
  // Tracks whichever source last filled the address text fields, so the
  // order actually carries real coordinates instead of always submitting
  // null — the two effects below are what set this, mirroring exactly
  // which fields they set alongside.
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(null);
  // AAD-MOB-016: the pincode the current `coords` actually correspond to.
  // Editing the address text no longer invalidates coords at all (see the
  // Field below) — only a pincode change to a genuinely *different*,
  // complete pincode does, since that's the edit that actually means the
  // captured point is for the wrong place.
  const [coordsPincode, setCoordsPincode] = useState<string | null>(null);
  const [pickerVisible, setPickerVisible] = useState(false);

  const user = useSession((s) => s.user);
  // AAD-MOB-017: selected individually rather than `useLocationStore()` as a
  // whole, so this screen only re-renders on the specific fields it reads —
  // a `label` change elsewhere in the store (this screen never reads it)
  // would otherwise re-render this whole form on every location fetch tick.
  const locationStatus = useLocationStore((s) => s.status);
  const locationLine1 = useLocationStore((s) => s.line1);
  const locationPincode = useLocationStore((s) => s.pincode);
  const locationLatitude = useLocationStore((s) => s.latitude);
  const locationLongitude = useLocationStore((s) => s.longitude);
  const requestLocation = useLocationStore((s) => s.request);

  // Fill from the first saved address first; only reach for device location
  // if there isn't one, and never overwrite something the person has already
  // started typing. Matches the chip selection below picking the same
  // address by default (see selectAddress), so the form and the chip row
  // agree about which one is "selected" from the moment this screen opens.
  useEffect(() => {
    if (prefilled) return;
    const saved = user?.addresses?.[0];
    if (saved?.line1) {
      setLine1(saved.line1);
      setLandmark(saved.landmark ?? '');
      setPincode(saved.pincode ?? '');
      if (saved.latitude != null && saved.longitude != null) {
        setCoords({ latitude: saved.latitude, longitude: saved.longitude });
        setCoordsPincode(saved.pincode ?? null);
      }
      setSelectedAddressLabel(saved.label);
      setPrefilled(true);
    }
  }, [user, prefilled]);

  // AAD-MOB-022: pick one of the saved addresses shown in the chip row
  // below. Marks prefilled too, same as the initial-load effect above, so
  // that effect never overwrites a deliberate choice if it somehow hasn't
  // fired yet.
  const selectAddress = (saved: Address) => {
    setLine1(saved.line1);
    setLandmark(saved.landmark ?? '');
    setPincode(saved.pincode ?? '');
    if (saved.latitude != null && saved.longitude != null) {
      setCoords({ latitude: saved.latitude, longitude: saved.longitude });
      setCoordsPincode(saved.pincode ?? null);
    } else {
      setCoords(null);
      setCoordsPincode(null);
    }
    setSelectedAddressLabel(saved.label);
    setPrefilled(true);
  };

  // AAD-MOB-018: 'located_no_address' means the GPS fix succeeded but the
  // reverse-geocode step didn't — there's no `locationLine1` to fill in, but
  // the coordinates are still real and worth keeping so the pin lands in
  // the right place even while the address line is typed by hand.
  const useCurrentLocation = () => {
    if (
      (locationStatus === 'found' || locationStatus === 'located_no_address') &&
      locationLatitude != null && locationLongitude != null
    ) {
      if (locationLine1) setLine1(locationLine1);
      if (locationPincode) setPincode(locationPincode);
      setCoords({ latitude: locationLatitude, longitude: locationLongitude });
      setCoordsPincode(locationPincode ?? null);
      setPrefilled(true);
      setSelectedAddressLabel(null);
    } else {
      void requestLocation();
    }
  };

  const applyPickedLocation = (picked: PickedLocation) => {
    setCoords({ latitude: picked.latitude, longitude: picked.longitude });
    setCoordsPincode(picked.pincode ?? null);
    if (picked.line1) setLine1(picked.line1);
    if (picked.pincode) setPincode(picked.pincode);
    setPrefilled(true);
    setSelectedAddressLabel(null);
    setPickerVisible(false);
  };

  // If the location finishes fetching after the button was already tapped
  // once (first tap only requests permission), apply it as soon as it lands.
  // AAD-MOB-025: used to depend on `locationStatus` alone while also reading
  // `line1`/`locationLine1`/`locationPincode`/`locationLatitude`/
  // `locationLongitude` from the closure — safe today only because every one
  // of those store fields is written atomically alongside `status` in
  // location.ts's own `set(...)` calls, so `status` happens to change
  // whenever they do. That's an invariant of the store, not of this effect,
  // and nothing enforced it: a future change to location.ts that updated one
  // of those fields without also changing `status` would silently stop
  // applying here, with the stale value sitting unused in a closure that
  // never re-ran. Depending on everything actually read removes that trap;
  // `line1` re-running the effect on every keystroke is harmless since the
  // `line1.trim().length === 0` gate is the first thing checked and turns
  // most of those runs into a no-op.
  useEffect(() => {
    if (
      (locationStatus === 'found' || locationStatus === 'located_no_address') &&
      locationLatitude != null && locationLongitude != null &&
      line1.trim().length === 0
    ) {
      if (locationLine1) setLine1(locationLine1);
      if (locationPincode) setPincode(locationPincode);
      setCoords({ latitude: locationLatitude, longitude: locationLongitude });
      setCoordsPincode(locationPincode ?? null);
    }
  }, [locationStatus, locationLine1, locationPincode, locationLatitude, locationLongitude, line1]);

  // AAD-MOB-016: a *complete*, genuinely different pincode is the edit that
  // invalidates a captured point — a still-in-progress edit (fewer than 6
  // digits) isn't, so this doesn't clear coords on every keystroke while
  // someone is mid-correction.
  const updatePincode = (t: string) => {
    setPincode(t);
    setSelectedAddressLabel(null);
    const trimmed = t.trim();
    if (coords && coordsPincode && /^\d{6}$/.test(trimmed) && trimmed !== coordsPincode) {
      setCoords(null);
      setCoordsPincode(null);
    }
  };

  const lines = useMemo(() => cartLines(items), [items]);

  const quote = useQuery({
    queryKey: ['quote', lines],
    queryFn: () => cartApi.quote(lines),
    enabled: lines.length > 0,
  });

  const placeOrder = useMutation({
    mutationFn: () => {
      const address: Address = {
        // AAD-MOB-022: the real label of whichever saved address is
        // currently selected (see selectAddress / the chip row below), or
        // the shared default when the address was typed/detected by hand
        // instead and was never one of the saved ones.
        label: selectedAddressLabel ?? DEFAULT_ADDRESS_LABEL,
        line1: line1.trim(),
        line2: '',
        landmark: landmark.trim(),
        city: SERVICE_CITY,
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
      <ErrorState error={quote.error} onRetry={() => void quote.refetch()} />
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

        {/* AAD-MOB-022: pick one of the saved addresses, or fall through to
            the location buttons / manual fields below for anything else —
            picking a chip fills those same fields rather than replacing
            them with a separate read-only summary, so editing a saved
            address's details for just this one order still works exactly
            like it always did. */}
        {(user?.addresses?.length ?? 0) > 0 && (
          <ScrollView
            horizontal
            showsHorizontalScrollIndicator={false}
            contentContainerStyle={styles.addressChipRow}
          >
            {user!.addresses.map((saved) => {
              const selected = selectedAddressLabel === saved.label;
              return (
                <Pressable
                  key={saved.label}
                  onPress={() => selectAddress(saved)}
                  accessibilityRole="button"
                  accessibilityState={{ selected }}
                  style={[styles.addressChip, selected && styles.addressChipSelected]}
                >
                  <Text
                    style={[styles.addressChipText, selected && styles.addressChipTextSelected]}
                  >
                    {saved.label}
                  </Text>
                </Pressable>
              );
            })}
          </ScrollView>
        )}

        <Pressable
          onPress={useCurrentLocation}
          accessibilityRole="button"
          style={({ pressed }) => [styles.locationButton, pressed && styles.locationButtonPressed]}
        >
          <Text style={styles.locationButtonText}>
            {locationStatus === 'locating' ? '📍 Finding your location…' : '📍 Use my current location'}
          </Text>
        </Pressable>
        <Pressable
          onPress={() => setPickerVisible(true)}
          accessibilityRole="button"
          style={({ pressed }) => [styles.mapButton, pressed && styles.locationButtonPressed]}
        >
          <Text style={styles.mapButtonText}>🗺️ Pin the exact spot on a map</Text>
        </Pressable>

        <Field
          label="Flat, building and street"
          value={line1}
          onChangeText={(t) => { setLine1(t); setSelectedAddressLabel(null); }}
          placeholder="12-3-45, Rose Villa, Banjara Hills"
          autoComplete="street-address"
        />
        <Field
          label="Landmark (optional)"
          value={landmark}
          onChangeText={(t) => { setLandmark(t); setSelectedAddressLabel(null); }}
          placeholder="Opposite the temple"
        />
        <Field
          label="Pincode"
          value={pincode}
          onChangeText={updatePincode}
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

      <LocationPickerModal
        visible={pickerVisible}
        initialCoords={coords ?? (locationLatitude != null && locationLongitude != null
          ? { latitude: locationLatitude, longitude: locationLongitude }
          : null)}
        onConfirm={applyPickedLocation}
        onClose={() => setPickerVisible(false)}
      />
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
  addressChipRow: { gap: space.sm, paddingBottom: space.xs },
  addressChip: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingVertical: space.sm,
    paddingHorizontal: space.md,
  },
  addressChipSelected: { backgroundColor: color.primarySoft, borderColor: color.primary },
  addressChipText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.ink },
  addressChipTextSelected: { color: color.primary },
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
  mapButton: {
    backgroundColor: color.primarySoft,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.primary,
    paddingVertical: space.sm,
    paddingHorizontal: space.md,
    alignItems: 'center',
  },
  mapButtonText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.primary },
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
