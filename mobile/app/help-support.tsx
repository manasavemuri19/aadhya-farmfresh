import { StyleSheet, View } from 'react-native';

import { HelpChatTree } from '../src/components/HelpChatTree';
import { HELP_TREE } from '../src/support/helpTree';
import { color, space } from '../src/theme/tokens';

export default function HelpSupportScreen() {
  return (
    <View style={styles.screen}>
      <HelpChatTree tree={HELP_TREE} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface, padding: space.lg },
});
