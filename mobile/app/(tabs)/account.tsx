import { ScrollView, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { useSession } from '../../src/store/session';
import { useCart } from '../../src/store/cart';
import { API_BASE_URL } from '../../src/api/client';
import { color, radius, space } from '../../src/theme/tokens';

export default function AccountScreen() {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { user, status, signOut } = useSession();
  const clearCart = useCart((s) => s.clear);

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={[styles.content, { paddingTop: insets.top + space.lg }]}
    >
      <Text variant="display">Account</Text>

      {status === 'signed_in' && user ? (
        <>
          <View style={styles.card}>
            <Text variant="caption">Signed in as</Text>
            <Text variant="title">{user.name || user.phone}</Text>
            {user.name ? <Text variant="caption">{user.phone}</Text> : null}
          </View>

          <Button
            label="Sign out"
            variant="secondary"
            onPress={() => {
              clearCart();
              void signOut();
            }}
          />
        </>
      ) : (
        <View style={styles.card}>
          <Text variant="body">Sign in to place an order and track deliveries.</Text>
          <Button
            label="Sign in"
            style={styles.signIn}
            onPress={() => router.push('/auth/phone')}
          />
        </View>
      )}

      <View style={styles.card}>
        <Text variant="caption">Aadhya Pickles &amp; Dairy</Text>
        <Text variant="body">
          Milk, curd, paneer, bilona ghee and homemade Andhra pickles, delivered
          across Hyderabad.
        </Text>
      </View>

      {__DEV__ && (
        <Text variant="caption" style={styles.debug}>
          API: {API_BASE_URL}
        </Text>
      )}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  card: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
    gap: space.sm,
  },
  signIn: { marginTop: space.sm },
  debug: { textAlign: 'center', marginTop: space.lg },
});
