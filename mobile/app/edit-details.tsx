import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput } from 'react-native';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { LocationPickerModal, type PickedLocation } from '../src/components/LocationPickerModal';
import { authApi } from '../src/api/endpoints';
import { useSession } from '../src/store/session';
import { useLocationStore } from '../src/store/location';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address } from '../src/api/types';

/**
 * The name / phone / address form that used to sit directly on the Profile
 * tab, now its own screen — reached via "Edit details" so the tab itself
 * reads as a short menu rather than a form on first open.
 */
export default function EditDetailsScreen() {
  const { user, setUser } = useSession();
  // AAD-MOB-017: individual selectors rather than the whole store — see
  // checkout.tsx for the same fix and why.
  const locationStatus = useLocationStore((s) => s.status);
  const locationLine1 = useLocationStore((s) => s.line1);
  const locationPincode = useLocationStore((s) => s.pincode);
  const locationLatitude = useLocationStore((s) => s.latitude);
  const locationLongitude = useLocationStore((s) => s.longitude);
  const requestLocation = useLocationStore((s) => s.request);

  const existing = user?.addresses?.[0];
  const [name, setName] = useState(user?.name ?? '');
  const [phone, setPhone] = useState(user?.phone ?? '');
  const [line1, setLine1] = useState(existing?.line1 ?? '');
  const [landmark, setLandmark] = useState(existing?.landmark ?? '');
  const [pincode, setPincode] = useState(existing?.pincode ?? '');
  // Starts from whatever the saved address already has (if any). Cleared
  // the moment the person hand-edits line1/pincode below, same reasoning
  // as checkout.tsx: stale coordinates describing the wrong place are
  // worse than none at all.
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(
    existing?.latitude != null && existing?.longitude != null
      ? { latitude: existing.latitude, longitude: existing.longitude }
      : null,
  );
  // Only replaced when the person edits the pincode to a genuinely
  // different one, or taps "use my current location" — otherwise the
  // saved address's existing coordinates travel through unchanged, even
  // while the address text itself is being corrected (AAD-MOB-016).
  const [coordsPincode, setCoordsPincode] = useState<string | null>(
    existing?.latitude != null && existing?.longitude != null ? (existing?.pincode ?? null) : null,
  );
  const [saved, setSaved] = useState(false);
  const [pickerVisible, setPickerVisible] = useState(false);

  const useCurrentLocation = () => {
    if (locationStatus === 'found' && locationLine1) {
      setLine1(locationLine1);
      if (locationPincode) setPincode(locationPincode);
      if (locationLatitude != null && locationLongitude != null) {
        setCoords({ latitude: locationLatitude, longitude: locationLongitude });
        setCoordsPincode(locationPincode ?? null);
      }
      setSaved(false);
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
    setSaved(false);
  };

  const updatePincode = (t: string) => {
    setPincode(t);
    setSaved(false);
    const trimmed = t.trim();
    if (coords && coordsPincode && /^\d{6}$/.test(trimmed) && trimmed !== coordsPincode) {
      setCoords(null);
      setCoordsPincode(null);
    }
  };

  // First tap only requests permission; if it resolves after that, apply it
  // as soon as it lands rather than making the person tap again.
  useEffect(() => {
    if (
      locationStatus === 'found' && locationLine1 &&
      line1.trim().length === 0
    ) {
      setLine1(locationLine1);
      if (locationPincode) setPincode(locationPincode);
      if (locationLatitude != null && locationLongitude != null) {
        setCoords({ latitude: locationLatitude, longitude: locationLongitude });
        setCoordsPincode(locationPincode ?? null);
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [locationStatus]);

  const save = useMutation({
    mutationFn: () => {
      const addressValid = line1.trim().length >= 4 && /^\d{6}$/.test(pincode.trim());
      return authApi.updateProfile({
        name: name.trim(),
        ...(phone.trim() ? { phone: phone.trim() } : {}),
        ...(addressValid
          ? {
              address: {
                label: 'Home',
                line1: line1.trim(),
                line2: '',
                landmark: landmark.trim(),
                city: 'Hyderabad',
                pincode: pincode.trim(),
                latitude: coords?.latitude ?? null,
                longitude: coords?.longitude ?? null,
              } as Address,
            }
          : {}),
      });
    },
    onSuccess: (profile) => {
      setUser(profile);
      setSaved(true);
    },
  });

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text variant="label" style={styles.sectionLabel}>Your name</Text>
        <TextInput
          value={name}
          onChangeText={(t) => { setName(t); setSaved(false); }}
          placeholder="e.g. Manasa"
          placeholderTextColor={color.muted}
          style={styles.input}
          accessibilityLabel="Your name"
          autoCapitalize="words"
        />

        <Text variant="label" style={styles.sectionLabel}>Mobile number</Text>
        <TextInput
          value={phone ?? ''}
          onChangeText={(t) => { setPhone(t); setSaved(false); }}
          placeholder="98765 43210"
          placeholderTextColor={color.muted}
          keyboardType="phone-pad"
          maxLength={10}
          style={styles.input}
          accessibilityLabel="Mobile number"
        />

        <Text variant="label" style={styles.sectionLabel}>Delivery address</Text>
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
        <TextInput
          value={line1}
          onChangeText={(t) => { setLine1(t); setSaved(false); }}
          placeholder="Flat, building and street"
          placeholderTextColor={color.muted}
          style={styles.input}
          accessibilityLabel="Address line"
        />
        <TextInput
          value={landmark}
          onChangeText={(t) => { setLandmark(t); setSaved(false); }}
          placeholder="Landmark (optional)"
          placeholderTextColor={color.muted}
          style={styles.input}
          accessibilityLabel="Landmark"
        />
        <TextInput
          value={pincode}
          onChangeText={updatePincode}
          placeholder="Pincode"
          placeholderTextColor={color.muted}
          style={styles.input}
          keyboardType="number-pad"
          maxLength={6}
          accessibilityLabel="Pincode"
        />

        {save.isError && (
          <Text style={styles.error}>
            {save.error instanceof Error ? save.error.message : 'Could not save.'}
          </Text>
        )}
        {saved && <Text style={styles.savedNote}>Saved</Text>}

        <Button
          label="Save"
          loading={save.isPending}
          disabled={name.trim().length === 0}
          onPress={() => save.mutate()}
          style={styles.saveButton}
        />
      </ScrollView>

      <LocationPickerModal
        visible={pickerVisible}
        initialCoords={coords}
        onConfirm={applyPickedLocation}
        onClose={() => setPickerVisible(false)}
      />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.sm, paddingBottom: space.xxl },
  sectionLabel: { marginTop: space.md, marginBottom: space.xs, fontSize: size.base },
  locationButton: {
    backgroundColor: color.leafSoft,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.leaf,
    paddingVertical: space.sm,
    paddingHorizontal: space.md,
    alignItems: 'center',
    marginBottom: space.xs,
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
    marginBottom: space.xs,
  },
  mapButtonText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.primary },
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
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount, marginTop: space.sm },
  savedNote: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.leaf, marginTop: space.sm },
  saveButton: { marginTop: space.lg },
});
