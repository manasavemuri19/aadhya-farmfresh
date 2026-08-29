import { View } from 'react-native';
import { Tabs } from 'expo-router';

import { useSession } from '../../src/store/session';
import { color, font } from '../../src/theme/tokens';

/**
 * Two tabs for a customer, three for staff/admin — "Update Stock" is
 * registered unconditionally (expo-router needs the route to exist) but
 * hidden from the bar via `href: null` for anyone without the role, rather
 * than being a second, separate navigator. One tree, one set of screens,
 * role only ever changes what's visible.
 */
export default function TabsLayout() {
  const role = useSession((s) => s.user?.role);
  const canManageStock = role === 'staff' || role === 'admin';

  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: color.primary,
        tabBarInactiveTintColor: color.muted,
        tabBarStyle: { backgroundColor: color.card, borderTopColor: color.line },
        tabBarLabelStyle: { fontFamily: font.bodyMedium, fontSize: 11 },
      }}
    >
      <Tabs.Screen
        name="index"
        options={{
          title: 'Order',
          tabBarIcon: ({ color: tint }) => <Dot color={tint} />,
        }}
      />
      <Tabs.Screen
        name="stock"
        options={{
          title: 'Update Stock',
          href: canManageStock ? undefined : null,
          tabBarIcon: ({ color: tint }) => <Dot color={tint} />,
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: 'Profile',
          tabBarIcon: ({ color: tint }) => <Dot color={tint} />,
        }}
      />
    </Tabs>
  );
}

// A plain dot in place of an icon-font dependency — keeps the tab bar from
// needing another package just for glyphs.
function Dot({ color: tint }: { color: string }) {
  return (
    <View style={{ width: 6, height: 6, borderRadius: 3, backgroundColor: tint }} />
  );
}
