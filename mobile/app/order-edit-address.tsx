import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { ErrorState, Loading } from '../src/components/Feedback';
import { LocationPickerModal, type PickedLocation } from '../src/components/LocationPickerModal';
import { ordersApi } from '../src/api/endpoints';
import { useLocationStore } from '../src/store/location';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address, OrderView } from '../src/api/types';

/**
 * A flat route (not nested under order/[id]) so it can be pushed from the
 * order screen with a plain `?orderId=` param, the same shape as the rest
 * of this app's one-off screens (edit-details, payment-callback).
 *
 * Product decision: `can_edit_address` is now always false from the backend
 * (see OrderService.update_address's own docstring) — a placed order's
 * address can't be self-serve edited at all, at any status. The order
 * screen's "Change address" link is gated on that flag too, so this screen
 * is effectively unreachable in normal use; the blocked state below is kept
 * as a defensive fallback (a stale deep link, an old cached screen) rather
 * than assuming that link is the only way here.
 */
export default function OrderEditAddressScreen() {
  const { orderId } = useLocalSearchParams<{ orderId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  // AAD-MOB-017: individual selectors rather than the whole store — see
  // checkout.tsx for the same fix and why.
  const locationStatus = useLocationStore((s) => s.status);
  const locationLine1 = useLocationStore((s) => s.line1);
  const locationPincode = useLocationStore((s) => s.pincode);
  const locationLatitude = useLocationStore((s) => s.latitude);
  const locationLongitude = useLocationStore((s) => s.longitude);
  const requestLocation = useLocationStore((s) => s.request);

  const order = useQuery({
    queryKey: ['order', orderId],
    queryFn: () => ordersApi.get(orderId),
  });

  const [line1, setLine1] = useState('');
  const [landmark, setLandmark] = useState('');
  const [pincode, setPincode] = useState('');
  const [prefilled, setPrefilled] = useState(false);
  // Only replaced when the person edits the pincode to a genuinely
  // different one, or taps "use my current location" — otherwise the
  // order's existing coordinates (if any) travel through unchanged, even
  // while the address text itself is being corrected (AAD-MOB-016).
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(null);
  const [coordsPincode, setCoordsPincode] = useState<string | null>(null);
  const [pickerVisible, setPickerVisible] = useState(false);

  useEffect(() => {
    if (prefilled || !order.data) return;
    setLine1(order.data.address.line1);
    setLandmark(order.data.address.landmark);
    setPincode(order.data.address.pincode);
    if (order.data.address.latitude != null && order.data.address.longitude != null) {
      setCoords({ latitude: order.data.address.latitude, longitude: order.data.address.longitude });
      setCoordsPincode(order.data.address.pincode ?? null);
    }
    setPrefilled(true);
  }, [order.data, prefilled]);

  // AAD-MOB-018: 'located_no_address' means the GPS fix succeeded but the
  // reverse-geocode step didn't — no `locationLine1` to fill in, but the
  // coordinates are still real and worth keeping for a hand-typed address.
  const useCurrentLocation = () => {
    if (
      (locationStatus === 'found' || locationStatus === 'located_no_address') &&
      locationLatitude != null && locationLongitude != null
    ) {
      if (locationLine1) setLine1(locationLine1);
      if (locationPincode) setPincode(locationPincode);
      setCoords({ latitude: locationLatitude, longitude: locationLongitude });
      setCoordsPincode(locationPincode ?? null);
    } else {
      void requestLocation();
    }
  };

  const applyPickedLocation = (picked: PickedLocation) => {
    setCoords({ latitude: picked.latitude, longitude: picked.longitude });
    setCoordsPincode(picked.pincode ?? null);
    if (picked.line1) setLine1(picked.line1);
    if (picked.pincode) setPincode(picked.pincode);
    setPickerVisible(false);
  };

  const updatePincode = (t: string) => {
    setPincode(t);
    const trimmed = t.trim();
    if (coords && coordsPincode && /^\d{6}$/.test(trimmed) && trimmed !== coordsPincode) {
      setCoords(null);
      setCoordsPincode(null);
    }
  };

  const save = useMutation({
    mutationFn: () => {
      const current = order.data as OrderView;
      const address: Address = {
        label: current.address.label,
        line1: line1.trim(),
        line2: current.address.line2,
        landmark: landmark.trim(),
        city: current.address.city,
        pincode: pincode.trim(),
        latitude: coords?.latitude ?? null,
        longitude: coords?.longitude ?? null,
      };
      return ordersApi.updateAddress(orderId, address);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['order', orderId] });
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
      router.back();
    },
  });

  if (order.isPending) return <Loading />;
  if (order.isError) {
    return (
      <ErrorState error={order.error} onRetry={() => void order.refetch()} />
    );
  }

  if (!order.data.can_edit_address) {
    return (
      <View style={styles.blocked}>
        <Text variant="title">Can't change this here</Text>
        <Text variant="body" style={styles.blockedBody}>
          The delivery address for order {order.data.order_number} can't be changed once it's
          placed. Message us from Help & Support with the order number and the correct address
          and we'll try to catch it.
        </Text>
        <Button label="Back to order" variant="secondary" onPress={() => router.back()} />
      </View>
    );
  }

  const addressValid = line1.trim().length >= 4 && /^\d{6}$/.test(pincode.trim());

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text variant="title">Deliver order {order.data.order_number} to</Text>

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
          onChangeText={setLine1}
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
          onChangeText={updatePincode}
          placeholder="500034"
          keyboardType="number-pad"
          maxLength={6}
        />

        {save.isError && (
          <View style={styles.errorBox}>
            <Text style={styles.errorText}>
              {save.error instanceof Error ? save.error.message : 'Could not update the address.'}
            </Text>
          </View>
        )}
      </ScrollView>

      <View style={styles.footer}>
        <Button
          label="Save new address"
          disabled={!addressValid}
          loading={save.isPending}
          onPress={() => save.mutate()}
        />
      </View>

      <LocationPickerModal
        visible={pickerVisible}
        initialCoords={coords}
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

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  blocked: { flex: 1, backgroundColor: color.surface, padding: space.lg, gap: space.md, justifyContent: 'center' },
  blockedBody: { color: color.muted },
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
  errorBox: {
    backgroundColor: color.discountSoft,
    borderRadius: radius.md,
    padding: space.md,
    gap: space.xs,
  },
  errorText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  footer: {
    padding: space.lg,
    borderTopWidth: 1,
    borderTopColor: color.line,
    backgroundColor: color.card,
  },
});
