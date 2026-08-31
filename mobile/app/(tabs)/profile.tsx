import { Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { useSession } from '../../src/store/session';
import { useCart } from '../../src/store/cart';
import { color, font, radius, size, space } from '../../src/theme/tokens';

/**
 * Everything that used to live behind the hamburger menu, now its own tab:
 * who you are, order history, and links out to the rest (edit details,
 * help). The details form itself lives on its own screen now — see
 * app/edit-details.tsx — so this tab reads as a short menu, not a form.
 */
export default function ProfileTab() {
  const insets = useSafeAreaInsets();
  const router = useRouter();
  const { user, signOut } = useSession();
  const clearCart = useCart((s) => s.clear);

  return (
    <View style={styles.screen}>
      <ScrollView
        contentContainerStyle={[styles.content, { paddingTop: insets.top + space.lg }]}
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

        <Pressable
          onPress={() => router.push('/edit-details')}
          style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
        >
          <Text style={styles.rowLabel}>Edit details</Text>
        </Pressable>

        <Pressable
          onPress={() => router.push('/help-support')}
          style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
        >
          <Text style={styles.rowLabel}>Help & Support</Text>
        </Pressable>

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
    </View>
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
  signOutButton: { marginTop: space.sm },
});
