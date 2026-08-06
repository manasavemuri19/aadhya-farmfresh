import { StyleSheet, TextInput, View } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';

interface Props {
  value: string;
  onChangeText: (text: string) => void;
  placeholder?: string;
}

export function SearchBar({ value, onChangeText, placeholder = 'Search milk, paneer, pickles…' }: Props) {
  return (
    <View style={styles.wrap}>
      <Text style={styles.icon}>⌕</Text>
      <TextInput
        value={value}
        onChangeText={onChangeText}
        placeholder={placeholder}
        placeholderTextColor={color.muted}
        style={styles.input}
        returnKeyType="search"
        accessibilityLabel="Search products"
        clearButtonMode="while-editing"
      />
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    height: 48,
    gap: space.sm,
  },
  icon: { fontFamily: font.body, fontSize: size.lg, color: color.muted, marginTop: -2 },
  input: {
    flex: 1,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
    padding: 0,
  },
});
