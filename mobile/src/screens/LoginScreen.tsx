import { useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { Text } from '../components/Text';
import { Button } from '../components/Button';
import { authApi } from '../api/endpoints';
import { tokenStore } from '../store/tokenStore';
import { useSession } from '../store/session';
import { signInWithGoogle, isGoogleConfigured } from '../lib/googleAuth';
import { color, font, size, space } from '../theme/tokens';

/**
 * The entire app's front door. Rendered directly by the root layout when
 * signed out — before any Stack, any route, anything — so there is
 * structurally no way to reach a screen without going through this first.
 *
 * "Login" and "Register" are the same Google flow underneath (the backend
 * creates an account on first sign-in) — shown as two separate calls to
 * action anyway, because a first-time visitor and a returning customer are
 * looking for different words on the screen, even when the button does the
 * same thing.
 */
export function LoginScreen() {
  const insets = useSafeAreaInsets();
  const setUser = useSession((s) => s.setUser);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const configured = isGoogleConfigured();

  const handleSignIn = async () => {
    setError(null);
    setSubmitting(true);
    try {
      const result = await signInWithGoogle();
      if (!result) return; // cancelled
      const { tokens, user } = await authApi.googleSignIn(result.idToken);
      await tokenStore.save(tokens);
      setUser(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not sign in. Try again.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <View style={[styles.screen, { paddingTop: insets.top + space.xxl, paddingBottom: insets.bottom + space.xl }]}>
      <View style={styles.hero}>
        <Text variant="display" style={styles.wordmark}>Aadya</Text>
        <Text variant="caption" style={styles.tagline}>Pickles &amp; Dairy · Hyderabad</Text>
      </View>

      <View style={styles.actions}>
        {!configured && (
          <Text style={styles.warning}>Sign-in isn't configured yet on this build.</Text>
        )}

        {error && <Text style={styles.error}>{error}</Text>}

        <Button
          label="Continue with Google"
          loading={submitting}
          disabled={!configured}
          onPress={() => void handleSignIn()}
        />

        <Text style={styles.registerLink} onPress={() => void handleSignIn()}>
          New user? <Text style={styles.registerLinkBold}>Register with Google</Text>
        </Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: {
    flex: 1,
    backgroundColor: color.surface,
    paddingHorizontal: space.lg,
    justifyContent: 'space-between',
  },
  hero: { alignItems: 'center', marginTop: space.xxl * 2 },
  wordmark: { fontSize: 44 },
  tagline: { fontFamily: font.bodyMedium, marginTop: space.xs },
  actions: { gap: space.md, paddingBottom: space.xl },
  registerLink: {
    textAlign: 'center',
    fontFamily: font.body,
    fontSize: size.sm,
    color: color.body,
  },
  registerLinkBold: { fontFamily: font.bodyBold, color: color.primary },
  error: {
    fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount, textAlign: 'center',
  },
  warning: {
    fontFamily: font.bodyMedium, fontSize: size.sm, color: color.lowStock, textAlign: 'center',
  },
});
