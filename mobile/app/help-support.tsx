import { StyleSheet, View } from 'react-native';

import { Text } from '../src/components/Text';
import { color, space } from '../src/theme/tokens';

/**
 * Placeholder — content to be filled in later. Exists now so the row on the
 * Profile tab has somewhere real to go instead of a dead end.
 */
export default function HelpSupportScreen() {
  return (
    <View style={styles.screen}>
      <Text variant="title">Help & Support</Text>
      <Text variant="caption" style={styles.note}>Coming soon.</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface, padding: space.lg, gap: space.xs },
  note: { marginTop: space.xs },
});
