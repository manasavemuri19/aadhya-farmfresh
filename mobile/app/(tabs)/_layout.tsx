import { StyleSheet, View } from 'react-native';
import { Tabs, useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { useSession } from '../../src/store/session';
import { OrderTrackerBar, TRACKER_BAR_HEIGHT } from '../../src/components/OrderTrackerBar';
import { useActiveOrder } from '../../src/hooks/useActiveOrder';
import { color, font, space } from '../../src/theme/tokens';

// Extra room below the default safe-area inset. The stock system nav bar
// inset alone was leaving labels feeling cramped/clipped on several
// devices — this pads it out by roughly half an inch (~36dp) beyond that,
// split between a taller bar and more bottom padding.
const EXTRA_BOTTOM_SPACE = 36;

// Total vertical space the tracker bar occupies once mounted (its wrap's
// own top padding + the bar's rendered height) — exported so a screen with
// its own bottom-anchored bar (the Order tab's CartBar) can add this as
// extra offset instead of rendering underneath the tracker. See CartBar's
// `bottomOffset` prop.
export const TRACKER_BAR_SPACE = space.sm + TRACKER_BAR_HEIGHT;

/**
 * Three different tab sets share this one file, gated by role: customer
 * gets Order + Profile; staff/admin get Order + Update Stock + Profile;
 * a delivery agent gets Requests + Profile only. Every screen is
 * registered unconditionally (expo-router needs the route to exist) but
 * hidden from the bar via `href: null` for roles that shouldn't see it —
 * one tree, one set of screens, role only ever changes what's visible.
 *
 * Icons come from @expo/vector-icons, which ships as part of Expo's core
 * SDK — already linked in every existing build, so real icons here don't
 * cost a new native module or another `eas build`.
 */
export default function TabsLayout() {
  const role = useSession((s) => s.user?.role);
  const canManageStock = role === 'staff' || role === 'admin';
  const isDeliveryAgent = role === 'delivery_agent';
  const insets = useSafeAreaInsets();
  const router = useRouter();

  const tabBarHeight = 56 + insets.bottom + EXTRA_BOTTOM_SPACE;

  // Lives at the layout level, not inside a single tab screen, so it stays
  // pinned above the tab bar no matter which tab is open — the same way the
  // cart button in most delivery apps doesn't disappear when you switch
  // screens. Polling rather than push: good enough while there's no
  // websocket/push channel for order updates yet. Skipped entirely for a
  // delivery agent — this tracks the signed-in person's own placed orders,
  // which isn't what an agent is in the app to do. Shared with the Order
  // tab (see useActiveOrder) so its own cart bar can leave room for this.
  const activeOrder = useActiveOrder();

  return (
    <View style={styles.root}>
      <Tabs
        // initialRouteName is only read once, the very first time this
        // navigator mounts — if `role` isn't synchronously known yet on
        // that first render (a brief moment while the session is still
        // resolving), it locks in "index" regardless and never
        // re-evaluates, even after role becomes known a moment later. The
        // key forces a fresh mount — and a fresh read of initialRouteName
        // — the instant role goes from unknown to known.
        key={role ?? 'pending'}
        initialRouteName={isDeliveryAgent ? 'requests' : 'index'}
        screenOptions={{
          headerShown: false,
          tabBarActiveTintColor: color.primary,
          tabBarInactiveTintColor: color.muted,
          tabBarStyle: {
            backgroundColor: color.card,
            borderTopColor: color.line,
            height: tabBarHeight,
            paddingTop: 8,
            paddingBottom: insets.bottom + EXTRA_BOTTOM_SPACE,
          },
          tabBarLabelStyle: { fontFamily: font.bodyMedium, fontSize: 11 },
        }}
      >
        <Tabs.Screen
          name="index"
          options={{
            title: 'Order',
            href: isDeliveryAgent ? null : undefined,
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
          name="requests"
          options={{
            title: 'Requests',
            href: isDeliveryAgent ? undefined : null,
            tabBarIcon: ({ color: tint, focused }) => (
              <Ionicons name={focused ? 'bicycle' : 'bicycle-outline'} size={22} color={tint} />
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

      {activeOrder && (
        <View pointerEvents="box-none" style={[styles.trackerWrap, { bottom: tabBarHeight }]}>
          <OrderTrackerBar
            orderNumber={activeOrder.order_number}
            status={activeOrder.status}
            onPress={() => router.push(`/order/${activeOrder.id}`)}
          />
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1 },
  trackerWrap: {
    position: 'absolute',
    left: 0,
    right: 0,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
  },
});
