/**
 * AAD-MOB-003: shows which backend this build is actually talking to,
 * whenever it isn't production — the "nothing on screen indicates which
 * environment they're on" half of the finding. Rendered once, at the very
 * top of `app/_layout.tsx`, above the signed-out/signed-in/degraded switch,
 * so it's visible regardless of session state.
 *
 * Silent in a real production build (`apiEnvironment === 'production'`).
 * Everywhere else it shows the resolved environment label, in a louder form
 * specifically when nothing was configured at all (`apiBaseUrlIsFallback`) —
 * the "fail loudly in dev when config is missing" half of the same finding.
 */

import { StyleSheet, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { Text } from './Text';
import { getApiEnvironment, isApiBaseUrlUnconfigured } from '../api/client';
import { color, size, space } from '../theme/tokens';

export function EnvironmentBanner() {
  const insets = useSafeAreaInsets();
  const environment = getApiEnvironment();
  if (environment === 'production') return null;

  const unconfigured = isApiBaseUrlUnconfigured();

  return (
    <View
      pointerEvents="none"
      style={[
        styles.banner,
        { paddingTop: insets.top + space.xs },
        unconfigured ? styles.bannerWarning : null,
      ]}
    >
      <Text variant="caption" style={styles.text}>
        {unconfigured
          ? `⚠ No API URL configured — using ${environment} fallback`
          : environment.toUpperCase()}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  banner: {
    backgroundColor: color.lowStock,
    paddingBottom: space.xs,
    alignItems: 'center',
  },
  bannerWarning: {
    backgroundColor: color.discount,
  },
  text: {
    color: color.white,
    fontSize: size.xs,
    fontFamily: 'DMMono_500Medium',
    letterSpacing: 0.5,
  },
});
