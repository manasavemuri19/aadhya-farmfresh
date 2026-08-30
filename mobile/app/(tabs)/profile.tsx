import { useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { authApi } from '../../src/api/endpoints';
import { useSession } from '../../src/store/session';
import { useCart } from '../../src/store/cart';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { Address } from '../../src/api/types';

/**
 * Everything that used to live behind the hamburger menu, now its own tab:
 * who you are, your saved details, a link to order history, and sign out.
 */
export default function ProfileTab() {
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const { user, setUser, signOut } = useSession();
  const clearCart = useCart((s) => s.clear);

  const existing = user?.addresses?.[0];
  const [name, setName] = useState(user?.name ?? '');
  const [phone, setPhone] = useState(user?.phone ?? '');
  const [line1, setLine1] = useState(existing?.line1 ?? '');
  const [landmark, setLandmark] = useState(existing?.landmark ?? '');
  const [pincode, setPincode] = useState(existing?.pincode ?? '');
  const [saved, setSaved] = useState(false);

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
                latitude: null,
                longitude: null,
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
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: insets.top + space.lg }]}
        keyboardShouldPersistTaps="handled"
      >
        <Text variant="display" style={styles.heading}>Profile</Text>

        <View style={styles.identityCard}>
          <Text variant="title">{user?.name || 'Add your name'}</Text>
          {user?.email ? <Text variant="caption">{user.email}</Text> : null}
        </View>

        <Pressable
          onPress={() => router.push('/orders')}
          style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
        >
          <Text style={styles.rowLabel}>My orders</Text>
        </Pressable>

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

        <Button
          label="Sign out"
          variant="ghost"
          style={styles.signOutButton}
          onPress={() => {
            clearCart();
            void signOut();
          }}
        />
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.sm, paddingBottom: space.xxl },
  heading: { marginBottom: space.md },
  identityCard: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
    gap: space.xs,
    marginBottom: space.sm,
  },
  row: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.md,
  },
  rowPressed: { opacity: 0.7 },
  rowLabel: { fontFamily: font.bodyMedium, fontSize: size.md, color: color.ink },
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
  signOutButton: { marginTop: space.sm },
});
