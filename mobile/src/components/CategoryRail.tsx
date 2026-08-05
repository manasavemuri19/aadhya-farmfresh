import { Pressable, ScrollView, StyleSheet } from 'react-native';
import { Text } from './Text';
import { color, font, radius, size, space } from '../theme/tokens';
import type { Category } from '../api/types';

interface Props {
  categories: Category[];
  selected: string;
  onSelect: (slug: string) => void;
}

export function CategoryRail({ categories, selected, onSelect }: Props) {
  const all = [{ slug: 'all', name: 'All' }, ...categories];
  return (
    <ScrollView
      horizontal
      showsHorizontalScrollIndicator={false}
      contentContainerStyle={styles.row}
    >
      {all.map((category) => {
        const active = category.slug === selected;
        return (
          <Pressable
            key={category.slug}
            onPress={() => onSelect(category.slug)}
            accessibilityRole="tab"
            accessibilityState={{ selected: active }}
            style={[styles.tab, active && styles.tabActive]}
          >
            <Text style={[styles.label, active && styles.labelActive]}>{category.name}</Text>
          </Pressable>
        );
      })}
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  row: { gap: space.sm, paddingHorizontal: space.lg, paddingVertical: space.sm },
  tab: {
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
    borderRadius: radius.pill,
    backgroundColor: color.card,
    borderWidth: 1,
    borderColor: color.line,
  },
  tabActive: { backgroundColor: color.ink, borderColor: color.ink },
  label: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.body },
  labelActive: { color: color.white, fontFamily: font.bodyBold },
});
