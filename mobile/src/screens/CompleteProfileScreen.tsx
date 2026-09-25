import { useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../components/Text';
import { Button } from '../components/Button';
import { LocationPickerModal, type PickedLocation } from '../components/LocationPickerModal';
import { authApi } from '../api/endpoints';
import { useSession } from '../store/session';
import { useLocationStore } from '../store/location';
import { DEFAULT_ADDRESS_LABEL, SERVICE_CITY } from '../lib/address';
import { color, font, radius, size, space } from '../theme/tokens';
import type { Address } from '../api/types';

const PHONE_PATTERN = /^[6-9]\d{9}$/;
const PINCODE_PATTERN = /^\d{6}$/;

/**
 * Shown once, right after a first Google sign-in — before the tabs, before
 * anything else. Google gives us a name and an email; it does not give us a
 * phone number or an address, and both are required for every order this
 * business delivers. Rather than let someone reach checkout and discover
 * that gap there, it's collected up front, as a hard gate.
 */
export function CompleteProfileScreen() {
  const insets = useSafeAreaInsets();
  const { user, setUser, signOut } = useSession();
  // AAD-MOB-017: individual selectors rather than the whole store — see
  // checkout.tsx for the same fix and why.
  const locationStatus = useLocationStore((s) => s.status);
  const locationLine1 = useLocationStore((s) => s.line1);
  const locationPincode = useLocationStore((s) => s.pincode);
  const locationLatitude = useLocationStore((s) => s.latitude);
  const locationLongitude = useLocationStore((s) => s.longitude);
  const requestLocation = useLocationStore((s) => s.request);

  const [name, setName] = useState(user?.name ?? '');
  const [phone, setPhone] = useState(user?.phone ?? '');
  const [line1, setLine1] = useState('');
  const [landmark, setLandmark] = useState('');
  const [pincode, setPincode] = useState('');
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(null);
  // Only replaced when the person edits the pincode to a genuinely
  // different one, or taps "use my current location" — otherwise coords
  // travel through unchanged while the address text is being corrected
  // (AAD-MOB-016).
  const [coordsPincode, setCoordsPincode] = useState<string | null>(null);
  const [pickerVisible, setPickerVisible] = useState(false);

  const nameValid = name.trim().length >= 2;
  const phoneValid = PHONE_PATTERN.test(phone.trim());
  const addressValid = line1.trim().length >= 4 && PINCODE_PATTERN.test(pincode.trim());
  const canSave = nameValid && phoneValid && addressValid;

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
    if (coords && coordsPincode && PINCODE_PATTERN.test(trimmed) && trimmed !== coordsPincode) {
      setCoords(null);
      setCoordsPincode(null);
    }
  };

  const save = useMutation({
    mutationFn: () => {
      const address: Address = {
        // AAD-MOB-022: see src/lib/address.ts for why these two are
        // constants and not a fuller fix.
        label: DEFAULT_ADDRESS_LABEL,
        line1: line1.trim(),
        line2: '',
        landmark: landmark.trim(),
        city: SERVICE_CITY,
        pincode: pincode.trim(),
        latitude: coords?.latitude ?? null,
        longitude: coords?.longitude ?? null,
      };
      return authApi.updateProfile({ name: name.trim(), phone: phone.trim(), address });
    },
    onSuccess: (profile) => setUser(profile),
  });

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: insets.top + space.xl, paddingBottom: insets.bottom + space.xl }]}
        keyboardShouldPersistTaps="handled"
      >
        <Text variant="title">One more step</Text>
        <Text variant="body" style={styles.intro}>
          We need your name, number and delivery address before your first order.
        </Text>

        <Text variant="label" style={styles.sectionLabel}>Your name</Text>
        <TextInput
          value={name}
          onChangeText={setName}
          placeholder="e.g. Manasa"
          placeholderTextColor={color.muted}
          style={styles.input}
          autoCapitalize="words"
          accessibilityLabel="Your name"
        />

        <Text variant="label" style={styles.sectionLabel}>Mobile number</Text>
        <View style={styles.phoneRow}>
          <Text style={styles.prefix}>+91</Text>
          <TextInput
            value={phone}
            onChangeText={setPhone}
            placeholder="98765 43210"
            placeholderTextColor={color.muted}
            keyboardType="phone-pad"
            maxLength={10}
            style={styles.phoneInput}
            accessibilityLabel="Mobile number"
          />
        </View>

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
          onChangeText={setLine1}
          placeholder="Flat, building and street"
          placeholderTextColor={color.muted}
          style={styles.input}
          accessibilityLabel="Address line"
        />
        <TextInput
          value={landmark}
          onChangeText={setLandmark}
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
          keyboardType="number-pad"
          maxLength={6}
          style={styles.input}
          accessibilityLabel="Pincode"
        />

        {save.isError && (
          <Text style={styles.error}>
            {save.error instanceof Error ? save.error.message : 'Could not save. Try again.'}
          </Text>
        )}

        <Button
          label="Continue"
          disabled={!canSave}
          loading={save.isPending}
          onPress={() => save.mutate()}
          style={styles.saveButton}
        />

        <Text style={styles.signOutLink} onPress={() => void signOut()}>
          Sign out and use a different account
        </Text>
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
  content: { paddingHorizontal: space.lg, gap: space.sm },
  intro: { marginBottom: space.sm },
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
  phoneRow: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 50,
    gap: space.sm,
  },
  prefix: { fontFamily: font.mono, fontSize: size.md, color: color.muted },
  phoneInput: { flex: 1, fontFamily: font.mono, fontSize: size.md, color: color.ink },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount, marginTop: space.sm },
  saveButton: { marginTop: space.lg },
  signOutLink: {
    textAlign: 'center', marginTop: space.lg,
    fontFamily: font.body, fontSize: size.sm, color: color.muted,
  },
});
