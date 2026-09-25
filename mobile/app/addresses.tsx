import { useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { EmptyState } from '../src/components/Feedback';
import { authApi } from '../src/api/endpoints';
import { useSession } from '../src/store/session';
import { color, font, radius, size, space } from '../src/theme/tokens';

/**
 * AAD-MOB-022 (multi-address support): the management screen a saved
 * address never had one of before — `user.addresses` already came back
 * from the server as a real list (the backend has stored more than one
 * per account since before this batch), this is the first screen that
 * actually shows more than the first entry, or lets a customer add,
 * rename, or remove one.
 *
 * Reads straight from `useSession`'s already-loaded profile rather than
 * its own query — the list is already in memory, and every mutation below
 * refreshes it via `authApi.me()` + `setUser` on success, the same
 * "re-fetch the profile after a profile-shaped write" pattern edit-details.tsx
 * already uses for name/phone/address.
 */
export default function AddressesScreen() {
  const router = useRouter();
  const { user, setUser } = useSession();
  const [deletingLabel, setDeletingLabel] = useState<string | null>(null);

  const remove = useMutation({
    mutationFn: (label: string) => authApi.deleteAddress(label),
    onMutate: (label) => setDeletingLabel(label),
    onSuccess: async () => {
      setUser(await authApi.me());
    },
    onSettled: () => setDeletingLabel(null),
  });

  const confirmRemove = (label: string) => {
    Alert.alert(
      `Remove "${label}"?`,
      "This saved address will be removed. You'll still be able to add it again later.",
      [
        { text: 'Cancel', style: 'cancel' },
        { text: 'Remove', style: 'destructive', onPress: () => void remove.mutate(label) },
      ],
    );
  };

  const addresses = user?.addresses ?? [];

  return (
    <View style={styles.screen}>
      <ScrollView contentContainerStyle={styles.content}>
        {addresses.length === 0 ? (
          <EmptyState
            title="No saved addresses yet"
            message="Add one so you don't have to type it out every time you order."
            actionLabel="Add an address"
            onAction={() => router.push('/address-edit')}
          />
        ) : (
          addresses.map((address) => (
            <View key={address.label} style={styles.card}>
              <Pressable
                onPress={() => router.push(`/address-edit?label=${encodeURIComponent(address.label)}`)}
                accessibilityRole="button"
                style={styles.cardBody}
              >
                <Text variant="label">{address.label}</Text>
                <Text variant="body" style={styles.line}>{address.line1}</Text>
                {address.landmark ? (
                  <Text variant="caption" style={styles.line}>{address.landmark}</Text>
                ) : null}
                <Text variant="caption" style={styles.line}>
                  {address.city} {address.pincode}
                </Text>
              </Pressable>
              <Pressable
                onPress={() => confirmRemove(address.label)}
                accessibilityRole="button"
                accessibilityLabel={`Remove ${address.label}`}
                disabled={remove.isPending && deletingLabel === address.label}
                style={({ pressed }) => [styles.removeButton, pressed && styles.removeButtonPressed]}
              >
                <Text style={styles.removeButtonText}>
                  {remove.isPending && deletingLabel === address.label ? 'Removing…' : 'Remove'}
                </Text>
              </Pressable>
            </View>
          ))
        )}

        {remove.isError && (
          <Text style={styles.error}>
            {remove.error instanceof Error ? remove.error.message : 'Could not remove that address.'}
          </Text>
        )}
      </ScrollView>

      {addresses.length > 0 && (
        <View style={styles.footer}>
          <Button label="+ Add new address" variant="secondary" onPress={() => router.push('/address-edit')} />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  card: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    flexDirection: 'row',
    alignItems: 'center',
  },
  cardBody: { flex: 1, padding: space.md, gap: 2 },
  line: { color: color.muted },
  removeButton: { paddingHorizontal: space.md, paddingVertical: space.sm },
  removeButtonPressed: { opacity: 0.6 },
  removeButtonText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  footer: {
    padding: space.lg,
    borderTopWidth: 1,
    borderTopColor: color.line,
    backgroundColor: color.card,
  },
});
