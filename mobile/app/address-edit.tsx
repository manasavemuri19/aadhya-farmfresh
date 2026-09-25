import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { LocationPickerModal, type PickedLocation } from '../src/components/LocationPickerModal';
import { authApi } from '../src/api/endpoints';
import { useSession } from '../src/store/session';
import { useLocationStore } from '../src/store/location';
import { SERVICE_CITY } from '../src/lib/address';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address } from '../src/api/types';

/**
 * AAD-MOB-022 (multi-address support): add *or* edit a saved address —
 * reached from addresses.tsx either bare (add a new one) or with
 * `?label=` (edit/rename an existing one). Same location-detection and
 * pin-picker UX as checkout.tsx / edit-details.tsx / order-edit-address.tsx
 * (each of those has its own copy of this same block — this is a fifth,
 * following the same established pattern rather than pulling all of them
 * into one shared component, which would be a larger, separately-risky
 * refactor nobody asked for here).
 *
 * The one thing those three don't have: a `label` field, and the branch on
 * save between "plain save" and "rename" (see the `save` mutation below).
 */
export default function AddressEditScreen() {
  const { label: existingLabel } = useLocalSearchParams<{ label?: string }>();
  const router = useRouter();
  const { user, setUser } = useSession();
  const locationStatus = useLocationStore((s) => s.status);
  const locationLine1 = useLocationStore((s) => s.line1);
  const locationPincode = useLocationStore((s) => s.pincode);
  const locationLatitude = useLocationStore((s) => s.latitude);
  const locationLongitude = useLocationStore((s) => s.longitude);
  const requestLocation = useLocationStore((s) => s.request);

  const isEditing = Boolean(existingLabel);
  const existing = existingLabel ? user?.addresses?.find((a) => a.label === existingLabel) : undefined;

  const [label, setLabel] = useState(existing?.label ?? '');
  const [line1, setLine1] = useState(existing?.line1 ?? '');
  const [landmark, setLandmark] = useState(existing?.landmark ?? '');
  const [pincode, setPincode] = useState(existing?.pincode ?? '');
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(
    existing?.latitude != null && existing?.longitude != null
      ? { latitude: existing.latitude, longitude: existing.longitude }
      : null,
  );
  const [coordsPincode, setCoordsPincode] = useState<string | null>(
    existing?.latitude != null && existing?.longitude != null ? (existing?.pincode ?? null) : null,
  );
  const [pickerVisible, setPickerVisible] = useState(false);
  // If the address this screen was opened for got removed from another
  // device/tab while this screen was open, `existing` above simply comes
  // back undefined on the next profile refresh — this only prefills once,
  // from whatever `existing` was on mount, same as edit-details.tsx's own
  // `existing` usage.
  const [prefilled, setPrefilled] = useState(Boolean(existing));

  useEffect(() => {
    if (prefilled || !isEditing || !existing) return;
    setLabel(existing.label);
    setLine1(existing.line1);
    setLandmark(existing.landmark ?? '');
    setPincode(existing.pincode ?? '');
    if (existing.latitude != null && existing.longitude != null) {
      setCoords({ latitude: existing.latitude, longitude: existing.longitude });
      setCoordsPincode(existing.pincode ?? null);
    }
    setPrefilled(true);
  }, [existing, isEditing, prefilled]);

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
    mutationFn: async () => {
      const address: Address = {
        label: label.trim(),
        line1: line1.trim(),
        line2: '',
        landmark: landmark.trim(),
        city: SERVICE_CITY,
        pincode: pincode.trim(),
        latitude: coords?.latitude ?? null,
        longitude: coords?.longitude ?? null,
      };
      // A rename (the label changed while editing an existing address)
      // needs the dedicated PATCH route — a plain PUT under the new label
      // would create a second address and leave the old one behind. Adding
      // a brand-new address, or editing one without touching its label,
      // both go through the same plain save the rest of the app already
      // uses.
      if (isEditing && existingLabel && existingLabel !== address.label) {
        await authApi.renameAddress(existingLabel, address);
      } else {
        await authApi.saveAddress(address);
      }
      setUser(await authApi.me());
    },
    onSuccess: () => router.back(),
  });

  const labelValid = label.trim().length >= 1;
  const addressValid = line1.trim().length >= 4 && /^\d{6}$/.test(pincode.trim());

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text variant="title">{isEditing ? 'Edit address' : 'Add a new address'}</Text>

        <Field
          label="Name this address"
          value={label}
          onChangeText={setLabel}
          placeholder="Home, Work, Mom's place…"
          maxLength={32}
          autoCapitalize="words"
        />

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
              {save.error instanceof Error ? save.error.message : 'Could not save that address.'}
            </Text>
          </View>
        )}
      </ScrollView>

      <View style={styles.footer}>
        <Button
          label="Save address"
          disabled={!labelValid || !addressValid}
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
