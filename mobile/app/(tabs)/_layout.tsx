import { Tabs } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';

import { useSession } from '../../src/store/session';
import { color, font } from '../../src/theme/tokens';

/**
 * Two tabs for a customer, three for staff/admin — "Update Stock" is
 * registered unconditionally (expo-router needs the route to exist) but
 * hidden from the bar via `href: null` for anyone without the role, rather
 * than being a second, separate navigator. One tree, one set of screens,
 * role only ever changes what's visible.
 *
 * Icons come from @expo/vector-icons, which ships as part of Expo's core
 * SDK — already linked in every existing build, so real icons here don't
 * cost a new native module or another `eas build`.
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
          tabBarIcon: ({ color: tint, focused }) => (
            <Ionicons name={focused ? 'bag' : 'bag-outline'} size={22} color={tint} />
          ),
        }}
      />
      <Tabs.Screen
        name="stock"
        options={{
          title: 'Update Stock',
          href: canManageStock ? undefined : null,
          tabBarIcon: ({ color: tint, focused }) => (
            <Ionicons name={focused ? 'clipboard' : 'clipboard-outline'} size={22} color={tint} />
          ),
        }}
      />
      <Tabs.Screen
        name="profile"
        options={{
          title: 'Profile',
          tabBarIcon: ({ color: tint, focused }) => (
            <Ionicons name={focused ? 'person' : 'person-outline'} size={22} color={tint} />
          ),
        }}
      />
    </Tabs>
  );
}
