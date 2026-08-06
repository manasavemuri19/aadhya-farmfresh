import { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { EmptyState } from '../src/components/Feedback';
import { authApi } from '../src/api/endpoints';
import { useSession } from '../src/store/session';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address } from '../src/api/types';
import { useRouter } from 'expo-router';

export default function ProfileScreen() {
  const router = useRouter();
  const { user, status, setUser } = useSession();

  const existing = user?.addresses?.[0];
  const [name, setName] = useState(user?.name ?? '');
  const [line1, setLine1] = useState(existing?.line1 ?? '');
  const [landmark, setLandmark] = useState(existing?.landmark ?? '');
  const [pincode, setPincode] = useState(existing?.pincode ?? '');
  const [saved, setSaved] = useState(false);

  const save = useMutation({
    mutationFn: async () => {
      const profile = await authApi.updateName(name.trim());
      if (line1.trim().length >= 4 && /^\d{6}$/.test(pincode.trim())) {
        const address: Address = {
          label: 'Home',
          line1: line1.trim(),
          line2: '',
          landmark: landmark.trim(),
          city: 'Hyderabad',
          pincode: pincode.trim(),
          latitude: null,
          longitude: null,
        };
        await authApi.saveAddress(address);
        return { ...profile, addresses: [address] };
      }
      return profile;
    },
    onSuccess: (profile) => {
      setUser(profile);
      setSaved(true);
    },
  });

  if (status !== 'signed_in') {
    return (
      <EmptyState
        title="Sign in first"
        message="Sign in to set your name and delivery address."
        actionLabel="Sign in"
        onAction={() => router.replace('/auth/phone')}
      />
    );
  }

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <View style={styles.phoneCard}>
          <Text variant="caption">Signed in as</Text>
          <Text variant="title">{user?.phone}</Text>
        </View>

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

        <Text variant="label" style={styles.sectionLabel}>Delivery address</Text>
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
          onChangeText={(t) => { setPincode(t); setSaved(false); }}
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
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.sm, paddingBottom: space.xxl },
  phoneCard: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
    gap: space.xs,
    marginBottom: space.md,
  },
  sectionLabel: { marginTop: space.md, marginBottom: space.xs, fontSize: size.base },
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
