import { useEffect, useRef, useState } from 'react';
import { Alert, AppState, FlatList, Pressable, StyleSheet, View } from 'react-native';
import type { AppStateStatus } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import * as Location from 'expo-location';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { EmptyState, ErrorState, Loading } from '../../src/components/Feedback';
import { VerifyDeliveryModal } from '../../src/components/VerifyDeliveryModal';
import { deliveryApi } from '../../src/api/endpoints';
import { ApiError } from '../../src/api/client';
import { formatPaise } from '../../src/lib/money';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { DeliveryOrderView, OrderStatus } from '../../src/api/types';

const ONGOING_LABEL: Partial<Record<OrderStatus, string>> = {
  confirmed: 'Preparing',
  packed: 'Packed — ready to collect',
  out_for_delivery: 'On the way',
};

// What this agent can move an order into next, from wherever it currently
// sits — mirrors _AGENT_ALLOWED_STATUSES on the backend. 'delivered' has no
// entry here because once it's set the order leaves the ongoing list
// entirely (see DeliveryRepository's _ONGOING_STATUSES), so there's nothing
// further to advance it to from this screen.
//
// AAD-SEC-027: out_for_delivery's target is no longer reachable through the
// plain status endpoint at all (updateStatus below 409s on 'delivered' —
// the backend only accepts it from verify-delivery) — `requiresCode` marks
// that one entry so the button below opens VerifyDeliveryModal instead of
// calling advanceStatus directly.
const NEXT_STATUS: Partial<
  Record<OrderStatus, { status: OrderStatus; label: string; requiresCode?: boolean }>
> = {
  confirmed: { status: 'packed', label: 'Mark as packed' },
  packed: { status: 'out_for_delivery', label: 'Start delivery' },
  out_for_delivery: { status: 'delivered', label: 'Mark delivered', requiresCode: true },
};

// AAD-MOB-012: the ceiling on how often a report is sent while the agent is
// actually moving, and the minimum distance that counts as "moved" at all —
// passed to watchPositionAsync as timeInterval/distanceInterval so the OS
// itself batches and dedupes reads instead of this screen polling on a
// fixed timer regardless of whether anything changed. Matching
// REQUESTS_POLL_INTERVAL_MS below keeps "how far is this from me" reasonably
// fresh without hammering either the device's location hardware or the
// network.
const LOCATION_REPORT_INTERVAL_MS = 60_000;
const LOCATION_DISTANCE_FILTER_M = 50;
const REQUESTS_POLL_INTERVAL_MS = 15_000;

/**
 * "Notify nearby agents" is implemented as polling, not push — there's no
 * push-notification infrastructure (APNs/FCM) wired into this app yet.
 * Every agent's Requests tab, while open, asks "what's new?" every 15s.
 * That's a real, disclosed gap, not a design choice: the moment push is
 * worth building, this poll becomes the fallback for a backgrounded app
 * instead of the only mechanism.
 */
export default function RequestsScreen() {
  const insets = useSafeAreaInsets();
  const queryClient = useQueryClient();
  const [locationDenied, setLocationDenied] = useState(false);
  const [accepting, setAccepting] = useState<string | null>(null);
  const [acceptError, setAcceptError] = useState<string | null>(null);
  // Declining doesn't need a backend call: with "first accept wins," no one
  // is waiting on this specific agent — the order just stays available for
  // anyone else exactly as if this agent had never seen it. This only ever
  // needs to hide the card on THIS device; it resets on next app launch (or
  // if the same request briefly drops off the poll and comes back), same as
  // dismissing a notification rather than actioning it.
  const [dismissedIds, setDismissedIds] = useState<Set<string>>(new Set());
  // Release and status-advance are two different actions that can both be
  // visible on the same "confirmed" card, so each tracks its own in-flight
  // order id (for which button shows its own busy label) while sharing one
  // error box below the Ongoing heading.
  const [releasingId, setReleasingId] = useState<string | null>(null);
  const [advancingId, setAdvancingId] = useState<string | null>(null);
  const [ongoingActionError, setOngoingActionError] = useState<string | null>(null);
  // AAD-SEC-027: which order the code-entry sheet is open for, if any —
  // VerifyDeliveryModal itself owns the code field, submit state and error
  // display; this screen only needs to know whether to render it.
  const [verifyingOrder, setVerifyingOrder] = useState<DeliveryOrderView | null>(null);
  const reportedOnce = useRef(false);
  // AAD-MOB-012: whether the app is actually in the foreground right now —
  // GPS reporting below stops the instant it isn't, rather than continuing
  // for as long as this tab merely stays mounted (which, in a tab
  // navigator, is essentially the whole shift).
  const [appActive, setAppActive] = useState(AppState.currentState === 'active');

  useEffect(() => {
    const subscription = AppState.addEventListener('change', (next: AppStateStatus) => {
      setAppActive(next === 'active');
    });
    return () => subscription.remove();
  }, []);

  const ongoing = useQuery({
    queryKey: ['delivery', 'ongoing'],
    queryFn: () => deliveryApi.listOngoing(),
    refetchInterval: REQUESTS_POLL_INTERVAL_MS,
  });

  const requests = useQuery({
    queryKey: ['delivery', 'requests'],
    queryFn: () => deliveryApi.listRequests(),
    refetchInterval: REQUESTS_POLL_INTERVAL_MS,
  });

  // AAD-MOB-012: this used to run for the whole time the Requests tab was
  // mounted — foregrounded or not, deliveries in hand or not — making GPS,
  // the single largest discretionary battery draw on the phone, run
  // continuously for an entire shift, including after the agent was done
  // for the day. It's now on only while there's an active delivery to
  // route by *and* the app is actually in the foreground, and it uses
  // watchPositionAsync's own distance filter instead of a fixed-interval
  // poll, so the OS can skip reporting a stationary agent altogether.
  const hasActiveDeliveries = (ongoing.data?.length ?? 0) > 0;

  useEffect(() => {
    if (!hasActiveDeliveries || !appActive) return;

    let cancelled = false;
    let subscription: Location.LocationSubscription | undefined;

    const start = async () => {
      const { status } = await Location.requestForegroundPermissionsAsync();
      if (cancelled) return;
      if (status !== 'granted') {
        setLocationDenied(true);
        return;
      }
      setLocationDenied(false);
      try {
        subscription = await Location.watchPositionAsync(
          {
            accuracy: Location.Accuracy.Balanced,
            timeInterval: LOCATION_REPORT_INTERVAL_MS,
            distanceInterval: LOCATION_DISTANCE_FILTER_M,
          },
          (position) => {
            if (cancelled) return;
            reportedOnce.current = true;
            deliveryApi
              .reportLocation(position.coords.latitude, position.coords.longitude)
              .then(() => {
                if (!cancelled) {
                  void queryClient.invalidateQueries({ queryKey: ['delivery', 'requests'] });
                }
              })
              .catch(() => {
                // A single failed report isn't fatal — watchPositionAsync
                // keeps delivering ticks; the next one tries again.
              });
          },
        );
      } catch {
        // Starting the watch itself failed (e.g. location services off at
        // the OS level) — nothing further to do until the next mount/state
        // change re-attempts it.
      }
    };

    void start();
    return () => {
      cancelled = true;
      subscription?.remove();
    };
  }, [hasActiveDeliveries, appActive, queryClient]);

  const accept = async (order: DeliveryOrderView) => {
    setAcceptError(null);
    setAccepting(order.id);
    try {
      await deliveryApi.accept(order.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['delivery', 'requests'] }),
        queryClient.invalidateQueries({ queryKey: ['delivery', 'ongoing'] }),
      ]);
    } catch (err) {
      setAcceptError(
        err instanceof ApiError && err.code === 'conflict'
          ? 'Someone else already accepted this one.'
          : err instanceof Error ? err.message : 'Could not accept — try again.',
      );
      // Either way, refresh the list: a taken order needs to disappear
      // whether this tap lost the race or just arrived stale.
      void queryClient.invalidateQueries({ queryKey: ['delivery', 'requests'] });
    } finally {
      setAccepting(null);
    }
  };

  // Only offered while the order is still just-confirmed — see
  // DeliveryRepository.release on the backend for why a packed or
  // out-for-delivery order can't be sent back to the pool this way; a
  // button that would just 409 isn't worth showing.
  const release = async (order: DeliveryOrderView) => {
    setOngoingActionError(null);
    setReleasingId(order.id);
    try {
      await deliveryApi.release(order.id);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['delivery', 'requests'] }),
        queryClient.invalidateQueries({ queryKey: ['delivery', 'ongoing'] }),
      ]);
    } catch (err) {
      setOngoingActionError(err instanceof Error ? err.message : 'Could not release this order — try again.');
    } finally {
      setReleasingId(null);
    }
  };

  const confirmRelease = (order: DeliveryOrderView) => {
    Alert.alert(
      "Can't deliver this order?",
      `Order #${order.order_number} will go back to the pool for another delivery partner to pick up.`,
      [
        { text: 'Never mind', style: 'cancel' },
        { text: "Can't deliver it", style: 'destructive', onPress: () => void release(order) },
      ],
    );
  };

  // Advances an order one step (packed → out for delivery → delivered).
  // The customer's own order screen picks the new status up on its next
  // poll — nothing needs to be pushed to it from here.
  const advanceStatus = async (order: DeliveryOrderView) => {
    const next = NEXT_STATUS[order.status];
    if (!next) return;
    setOngoingActionError(null);
    setAdvancingId(order.id);
    try {
      await deliveryApi.updateStatus(order.id, next.status);
      void queryClient.invalidateQueries({ queryKey: ['delivery', 'ongoing'] });
    } catch (err) {
      setOngoingActionError(err instanceof Error ? err.message : 'Could not update this order — try again.');
    } finally {
      setAdvancingId(null);
    }
  };

  if (ongoing.isPending || requests.isPending) return <Loading label="Loading requests" />;
  if (requests.isError) {
    return (
      <ErrorState error={requests.error} onRetry={() => void requests.refetch()} />
    );
  }

  const ongoingList = ongoing.data ?? [];
  const requestsList = (requests.data ?? []).filter((o: DeliveryOrderView) => !dismissedIds.has(o.id));

  return (
    <>
    <FlatList
      style={styles.screen}
      contentContainerStyle={[styles.content, { paddingTop: insets.top + space.lg }]}
      data={requestsList}
      keyExtractor={(o) => o.id}
      ListHeaderComponent={
        <View>
          <Text variant="display" style={styles.heading}>Requests</Text>

          {locationDenied && (
            <View style={styles.locationNotice}>
              <Text variant="caption" style={styles.locationNoticeText}>
                Location isn't shared yet, so requests below aren't sorted by distance. You can
                allow location access for this app in your phone's settings.
              </Text>
            </View>
          )}

          {!locationDenied && !hasActiveDeliveries && (
            <View style={styles.locationNotice}>
              <Text variant="caption" style={styles.locationNoticeText}>
                Location sharing is paused, so requests below are not sorted by distance. It
                resumes automatically once you accept a delivery.
              </Text>
            </View>
          )}

          {ongoingList.length > 0 && (
            <>
              <Text variant="label" style={styles.sectionLabel}>Ongoing</Text>
              {ongoingActionError && (
                <View style={styles.errorBox}>
                  <Text style={styles.errorText}>{ongoingActionError}</Text>
                </View>
              )}
              {ongoingList.map((order: DeliveryOrderView) => {
                const next = NEXT_STATUS[order.status];
                // Either action, on any card, blocks both buttons on THIS
                // card — a second tap can't fire while the first is still
                // in flight, but other cards stay fully usable.
                const busy = releasingId !== null || advancingId !== null;
                return (
                  <View key={order.id} style={[styles.card, styles.ongoingCard]}>
                    <RequestCardBody order={order} />
                    <View style={styles.ongoingBottomRow}>
                      <Text style={styles.ongoingStatus}>
                        {ONGOING_LABEL[order.status] ?? order.status}
                      </Text>
                      <View style={styles.ongoingActions}>
                        {order.status === 'confirmed' && (
                          <Pressable
                            onPress={() => confirmRelease(order)}
                            disabled={busy}
                            accessibilityRole="button"
                            accessibilityLabel={`Can't deliver order ${order.order_number}`}
                          >
                            <Text style={styles.releaseLink}>
                              {releasingId === order.id ? 'Releasing…' : "Can't deliver this"}
                            </Text>
                          </Pressable>
                        )}
                        {next && (
                          <Pressable
                            onPress={() =>
                              next.requiresCode
                                ? setVerifyingOrder(order)
                                : void advanceStatus(order)
                            }
                            disabled={busy}
                            accessibilityRole="button"
                            accessibilityLabel={`${next.label}, order ${order.order_number}`}
                          >
                            <Text style={styles.advanceLink}>
                              {advancingId === order.id ? 'Updating…' : next.label}
                            </Text>
                          </Pressable>
                        )}
                      </View>
                    </View>
                  </View>
                );
              })}
            </>
          )}

          <Text variant="label" style={styles.sectionLabel}>New requests</Text>
          {acceptError && (
            <View style={styles.errorBox}>
              <Text style={styles.errorText}>{acceptError}</Text>
            </View>
          )}
        </View>
      }
      renderItem={({ item }) => (
        <View style={styles.card}>
          <RequestCardBody order={item} />
          <View style={styles.actionRow}>
            <Button
              label="Decline"
              variant="ghost"
              disabled={accepting !== null}
              onPress={() => setDismissedIds((prev) => new Set(prev).add(item.id))}
              style={styles.declineButton}
            />
            <Button
              label={accepting === item.id ? 'Accepting…' : 'Accept'}
              loading={accepting === item.id}
              disabled={accepting !== null}
              onPress={() => void accept(item)}
              style={styles.acceptButton}
            />
          </View>
        </View>
      )}
      ListEmptyComponent={
        <EmptyState
          title="Nothing new right now"
          message="New paid orders near you will show up here as they come in."
        />
      }
    />
    <VerifyDeliveryModal
      order={verifyingOrder}
      onVerified={() => {
        setVerifyingOrder(null);
        void queryClient.invalidateQueries({ queryKey: ['delivery', 'ongoing'] });
      }}
      onClose={() => setVerifyingOrder(null)}
    />
    </>
  );
}

function RequestCardBody({ order }: { order: DeliveryOrderView }) {
  return (
    <View style={styles.cardBody}>
      <View style={styles.cardTopRow}>
        <Text style={styles.orderNumber}>#{order.order_number}</Text>
        <Text style={styles.distance}>
          {order.distance_km != null ? `~${order.distance_km} km away` : 'Distance unknown'}
        </Text>
      </View>
      <Text variant="body" style={styles.addressLine}>{order.address.line1}</Text>
      {order.address.landmark ? (
        <Text variant="caption">Near {order.address.landmark}</Text>
      ) : null}
      <Text variant="caption">{order.address.pincode}</Text>
      {order.notes ? <Text variant="caption" style={styles.notes}>Note: {order.notes}</Text> : null}
      <View style={styles.cardBottomRow}>
        <Text variant="caption">{order.item_count} item{order.item_count === 1 ? '' : 's'}</Text>
        <Text style={styles.total}>{formatPaise(order.total_paise)}</Text>
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { paddingHorizontal: space.lg, paddingBottom: space.xxl },
  heading: { marginBottom: space.md },
  sectionLabel: { marginTop: space.md, marginBottom: space.sm, fontSize: size.base },
  locationNotice: {
    backgroundColor: color.primarySoft,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.md,
  },
  locationNoticeText: { color: color.primaryPressed },
  card: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.md,
    borderWidth: 1,
    borderColor: color.line,
    gap: space.sm,
  },
  ongoingCard: { borderColor: color.leaf },
  cardBody: { gap: 2 },
  cardTopRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  orderNumber: { fontFamily: font.bodyBold, fontSize: size.base, color: color.ink },
  distance: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.leaf },
  addressLine: { marginTop: 2 },
  notes: { fontStyle: 'italic' },
  cardBottomRow: {
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', marginTop: space.xs,
  },
  total: { fontFamily: font.monoBold, fontSize: size.md, color: color.ink },
  ongoingBottomRow: {
    flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center',
  },
  ongoingStatus: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.leaf },
  ongoingActions: { flexDirection: 'row', alignItems: 'center', gap: space.md },
  releaseLink: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  advanceLink: { fontFamily: font.bodyBold, fontSize: size.sm, color: color.primary },
  actionRow: { flexDirection: 'row', gap: space.sm, marginTop: space.xs },
  declineButton: { flex: 1 },
  acceptButton: { flex: 2 },
  errorBox: {
    backgroundColor: color.discountSoft,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.md,
  },
  errorText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
});
