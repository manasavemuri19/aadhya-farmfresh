import { Modal, Pressable, StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useRouter } from 'expo-router';
import { Text } from './Text';
import { Button } from './Button';
import { color, font, radius, size, space } from '../theme/tokens';
import { useSession } from '../store/session';
import { useCart } from '../store/cart';

interface Props {
  visible: boolean;
  onClose: () => void;
}

/**
 * The single menu, opened from the ⋯ in the top right. Replaces a tab bar —
 * this is a one-page shop, so navigation lives here rather than eating a strip
 * of the screen. Holds who you are, your orders, and sign out.
 */
export function DrawerMenu({ visible, onClose }: Props) {
  const router = useRouter();
  const insets = useSafeAreaInsets();
  const { user, status, signOut } = useSession();
  const clearCart = useCart((s) => s.clear);

  const go = (path: string) => {
    onClose();
    router.push(path);
  };

  return (
    <Modal visible={visible} transparent animationType="fade" onRequestClose={onClose}>
      <Pressable style={styles.backdrop} onPress={onClose} accessibilityLabel="Close menu">
        <Pressable
          style={[styles.sheet, { paddingTop: insets.top + space.lg }]}
          onPress={(e) => e.stopPropagation()}
        >
          {status === 'signed_in' && user ? (
            <View style={styles.identity}>
              <Text variant="title">{user.name || 'Add your name'}</Text>
              <Text variant="caption">{user.phone}</Text>
              <Pressable onPress={() => go('/profile')} hitSlop={8}>
                <Text style={styles.editLink}>Edit profile & address →</Text>
              </Pressable>
            </View>
          ) : (
            <View style={styles.identity}>
              <Text variant="title">Welcome</Text>
              <Text variant="caption">Sign in to order and track deliveries</Text>
            </View>
          )}

          <View style={styles.divider} />

          {status === 'signed_in' ? (
            <>
              <MenuRow label="My orders" onPress={() => go('/orders')} />
              <MenuRow label="Profile & address" onPress={() => go('/profile')} />
              {(user?.role === 'staff' || user?.role === 'admin') && (
                <MenuRow label="Manage stock" onPress={() => go('/admin/stock')} />
              )}
              <View style={styles.divider} />
              <MenuRow
                label="Sign out"
                destructive
                onPress={() => {
                  clearCart();
                  void signOut();
                  onClose();
                }}
              />
            </>
          ) : (
            <Button label="Sign in" style={styles.signIn} onPress={() => go('/auth/phone')} />
          )}

          <View style={styles.footer}>
            <Text variant="caption">Aadya Pickles &amp; Dairy · Hyderabad</Text>
          </View>
        </Pressable>
      </Pressable>
    </Modal>
  );
}

function MenuRow({
  label, onPress, destructive = false,
}: { label: string; onPress: () => void; destructive?: boolean }) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="button"
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
    >
      <Text style={[styles.rowLabel, destructive && styles.rowDestructive]}>{label}</Text>
    </Pressable>
  );
}

const styles = StyleSheet.create({
  backdrop: { flex: 1, backgroundColor: 'rgba(44,32,19,0.35)', justifyContent: 'flex-start' },
  sheet: {
    backgroundColor: color.surface,
    borderBottomLeftRadius: radius.lg,
    borderBottomRightRadius: radius.lg,
    paddingHorizontal: space.lg,
    paddingBottom: space.xl,
  },
  identity: { gap: space.xs, paddingVertical: space.sm },
  editLink: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.primary, marginTop: space.xs },
  divider: { height: 1, backgroundColor: color.line, marginVertical: space.sm },
  row: { paddingVertical: space.md },
  rowPressed: { opacity: 0.6 },
  rowLabel: { fontFamily: font.bodyMedium, fontSize: size.md, color: color.ink },
  rowDestructive: { color: color.discount },
  signIn: { marginTop: space.sm },
  footer: { marginTop: space.lg, alignItems: 'center' },
});
