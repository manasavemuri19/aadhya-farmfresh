import { useEffect, useRef, useState } from 'react';
import { StyleSheet, TextInput, View } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';

interface Props {
  value: string;
  onChangeText: (text: string) => void;
  placeholder?: string;
}

// AAD-MOB-014: every keystroke here re-filters the whole catalog (OrderTab's
// `products` useMemo) and re-renders the grid. 200ms is short enough that
// it isn't noticeable as a delay, long enough to collapse a fast typist's
// several keystrokes into one filter pass instead of one per character.
const SEARCH_DEBOUNCE_MS = 200;

export function SearchBar({ value, onChangeText, placeholder = 'Search milk, paneer, pickles…' }: Props) {
  // The field itself stays locally controlled so typing never feels
  // delayed — only the (expensive) propagation to the parent is debounced.
  const [text, setText] = useState(value);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Follows an external reset (e.g. a "clear search" action elsewhere).
  // Deliberately a render-time adjustment, not a useEffect: an effect would
  // set state a render late, and setting state unconditionally on every
  // `value` change is exactly the cascading-render pattern this file's
  // debounce exists to avoid. This never fires from typing here, since
  // typing only ever changes `value` via the debounced call below, by
  // which point `lastValue`/`text` already match it.
  const [lastValue, setLastValue] = useState(value);
  if (value !== lastValue) {
    setLastValue(value);
    setText(value);
  }

  useEffect(() => {
    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
    };
  }, []);

  const handleChangeText = (next: string) => {
    setText(next);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => onChangeText(next), SEARCH_DEBOUNCE_MS);
  };

  return (
    <View style={styles.wrap}>
      <Text style={styles.icon}>⌕</Text>
      <TextInput
        value={text}
        onChangeText={handleChangeText}
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
