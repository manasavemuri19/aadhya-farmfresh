import { StyleSheet, View } from 'react-native';
import { Tabs, useRouter } from 'expo-router';
import { Ionicons } from '@expo/vector-icons';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useQuery } from '@tanstack/react-query';

import { useSession } from '../../src/store/session';
import { ordersApi } from '../../src/api/endpoints';
import { OrderTrackerBar } from '../../src/components/OrderTrackerBar';
import { color, font, space } from '../../src/theme/tokens';
import type { OrderStatus } from '../../src/api/types';

// Extra room below the default safe-area inset. The stock system nav bar
// inset alone was leaving labels feeling cramped/clipped on several
// devices — this pads it out by roughly half an inch (~36dp) beyond that,
// split between a taller bar and more bottom padding.
const EXTRA_BOTTOM_SPACE = 36;

// Placed once an order is paid/confirmed, until it's actually in the
// customer's hands — this is the window the tracker bar should cover.
const IN_PROGRESS_STATUSES: readonly OrderStatus[] = ['confirmed', 'packed', 'out_for_delivery'];

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
  // which isn't what an agent is in the app to do.
  const orders = useQuery({
    queryKey: ['orders'],
    queryFn: () => ordersApi.list(),
    refetchInterval: 15_000,
    enabled: !isDeliveryAgent,
  });
  const activeOrder = orders.data?.find((o: { status: OrderStatus }) =>
    IN_PROGRESS_STATUSES.includes(o.status));

  return (
    <View style={styles.root}>
      <Tabs
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
