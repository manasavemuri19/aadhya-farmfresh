import { useEffect, useState } from 'react';
import { Linking, Pressable, ScrollView, StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
// AAD-BIZ-006: useMutation/useQueryClient were only used by the customer
// cancel below, now commented out. Previous import:
// import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useQuery } from '@tanstack/react-query';
// AAD-MOB-029: no react-native-maps import on this screen any more — see
// the "Live location" card below for why.

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { ErrorState, Loading } from '../../src/components/Feedback';
// AAD-MOB-013: this screen renders status-keyed lookups (STEPS, COPY) and a
// map from server data, so an unexpected status value (AAD-DATA-010) or a
// stray null is representable here more than most screens — a per-route
// boundary means that failure replaces this screen only, not the whole
// stack the customer navigated through to reach it.
export { AppErrorFallback as ErrorBoundary } from '../../src/components/ErrorBoundary';
import { ordersApi } from '../../src/api/endpoints';
import { formatPaise } from '../../src/lib/money';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { AgentLocation, Address, OrderStatus, OrderView } from '../../src/api/types';

const STEPS: { status: OrderStatus; label: string }[] = [
  { status: 'confirmed', label: 'Confirmed' },
  { status: 'packed', label: 'Packed' },
  { status: 'out_for_delivery', label: 'On the way' },
  { status: 'delivered', label: 'Delivered' },
];

const COPY: Record<OrderStatus, string> = {
  pending_payment: 'Waiting for payment',
  confirmed: 'The farm is preparing your order',
  packed: 'Packed and ready to leave',
  out_for_delivery: 'On the way to you',
  delivered: 'Delivered',
  cancelled: 'Cancelled',
  refunded: 'Refunded',
};

// Ticks so the ETA line below counts down in real time instead of freezing
// at whatever `eta_minutes` was when the order was placed. 30s is plenty —
// the line only ever displays whole minutes, so anything shorter is wasted
// re-renders.
function useNow(intervalMs: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(id);
  }, [intervalMs]);
  return now;
}

/**
 * `eta_minutes` is a single number frozen on the order at checkout — the
 * slowest item's prep time (see pricing.compute_eta_minutes on the backend).
 * It is a promise made at confirmation time, not a live GPS/traffic ETA, so
 * the honest way to show it is as a countdown from the moment the order was
 * confirmed, not as a static "X minutes" that never changes. Once the
 * countdown runs out we stop naming a number — there's no live agent
 * location feed yet to say anything more precise than "any moment now."
 */
function etaText(data: OrderView, now: number): string | null {
  if (data.status === 'delivered' || data.status === 'cancelled' || data.status === 'refunded') {
    return null;
  }
  const confirmedAt = data.timeline.find((event) => event.status === 'confirmed')?.at;
  if (!confirmedAt) {
    // Still pending_payment — nothing confirmed yet to count down from.
    return `Arriving in about ${data.eta_minutes} minutes`;
  }
  const targetMs = new Date(confirmedAt).getTime() + data.eta_minutes * 60_000;
  const remainingMinutes = Math.round((targetMs - now) / 60_000);
  if (remainingMinutes >= 1) {
    return `Arriving in about ${remainingMinutes} minute${remainingMinutes === 1 ? '' : 's'}`;
  }
  return 'Arriving any moment now';
}

/**
 * Straight-line distance in km between the agent and the drop-off (haversine).
 * Not a road distance — an honest "roughly how far" for the tracking card,
 * computed on the phone so it needs no Maps API key or network call.
 */
function distanceKm(agent: AgentLocation, address: Address): number | null {
  if (address.latitude == null || address.longitude == null) return null;
  const toRad = (d: number) => (d * Math.PI) / 180;
  const dLat = toRad(address.latitude - agent.latitude);
  const dLon = toRad(address.longitude - agent.longitude);
  const a =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(agent.latitude)) * Math.cos(toRad(address.latitude)) * Math.sin(dLon / 2) ** 2;
  return 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

/**
 * Opens the agent's live position in the Google Maps app (or browser) —
 * as a route to the customer's address when coordinates are on file,
 * otherwise as a pin. A plain URL: no API key, no native module.
 */
function openAgentInMaps(agent: AgentLocation, address: Address): void {
  const from = `${agent.latitude},${agent.longitude}`;
  const url =
    address.latitude != null && address.longitude != null
      ? `https://www.google.com/maps/dir/?api=1&origin=${from}&destination=${address.latitude},${address.longitude}&travelmode=driving`
      : `https://www.google.com/maps/search/?api=1&query=${from}`;
  void Linking.openURL(url);
}

export default function OrderScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const router = useRouter();
  // const queryClient = useQueryClient(); // AAD-BIZ-006: only used by cancel
  const now = useNow(30_000);

  const order = useQuery({
    queryKey: ['order', id],
    queryFn: () => ordersApi.get(id),
    // Poll while the order is live. The webhook may confirm payment a moment
    // after the app returns from the gateway, so the screen catches up on its
    // own rather than leaving the customer to pull-to-refresh.
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      if (!status) return 5_000;
      return ['delivered', 'cancelled', 'refunded'].includes(status) ? false : 5_000;
    },
  });

  const agentLocation = order.data?.delivery_agent_location ?? null;

  // AAD-BIZ-006 (product decision): customers can't cancel a placed/paid
  // order from the app — Zepto/Blinkit model. The backend enforces this
  // (can_cancel is now always false past pending_payment, and the cancel
  // endpoint refuses), so this is just the UI half. Kept commented, not
  // deleted, in case the decision is reversed.
  // const cancel = useMutation({
  //   mutationFn: (reason: string) => ordersApi.cancel(id, reason),
  //   onSuccess: () => {
  //     void queryClient.invalidateQueries({ queryKey: ['order', id] });
  //     void queryClient.invalidateQueries({ queryKey: ['orders'] });
  //   },
  // });

  if (order.isPending) return <Loading />;
  if (order.isError) {
    return (
      <ErrorState error={order.error} onRetry={() => void order.refetch()} />
    );
  }

  const data: OrderView = order.data;
  const currentStep = STEPS.findIndex((s) => s.status === data.status);
  const stopped = data.status === 'cancelled' || data.status === 'refunded';
  const eta = etaText(data, now);

  return (
    <ScrollView style={styles.screen} contentContainerStyle={styles.content}>
      <View style={styles.hero}>
        <Text variant="caption">Order {data.order_number}</Text>
        <Text variant="title" style={styles.status}>{COPY[data.status]}</Text>
        {eta && (
          <Text variant="caption" style={styles.eta}>
            {eta}
          </Text>
        )}
      </View>

      {!stopped && (
        <View style={styles.track}>
          {STEPS.map((step, index) => {
            const done = currentStep >= index && currentStep !== -1;
            return (
              <View key={step.status} style={styles.trackStep}>
                <View style={[styles.dot, done && styles.dotDone]} />
                <Text style={[styles.trackLabel, done && styles.trackLabelDone]}>
                  {step.label}
                </Text>
              </View>
            );
          })}
        </View>
      )}

      {/* AAD-SEC-027: in-app proof-of-delivery. Shown only while the backend
          actually considers the code usable (out_for_delivery, unexpired —
          see OrderService._delivery_code_if_usable), so this simply follows
          `data.delivery_code` rather than adding its own status/expiry
          logic on top. */}
      {data.delivery_code && (
        <View style={styles.card}>
          <Text variant="label" style={styles.cardTitle}>Delivery code</Text>
          <Text variant="caption">
            Read this out to your delivery partner when they arrive — it&apos;s how we confirm
            the order reached you.
          </Text>
          <Text
            style={styles.deliveryCode}
            numberOfLines={1}
            adjustsFontSizeToFit
            minimumFontScale={0.6}
          >
            {data.delivery_code}
          </Text>
        </View>
      )}

      {data.status === 'out_for_delivery' && (
        <View style={styles.card}>
          <Text variant="label" style={styles.cardTitle}>Live location</Text>
          {/* AAD-MOB-029: this used to be an embedded react-native-maps
              MapView (Google provider). That native view hard-crashes the
              whole app on Android — not a JS error, so no ErrorBoundary can
              catch it — when the APK was built without a Google Maps API
              key, and this build has none: the old key leaked via the repo
              and was revoked (see app.config.js). It only started crashing
              now because agent location reporting finally works (the
              permission-loop fix), so `agentLocation` is non-null and the
              MapView actually renders. Same root cause as the original
              "pin my location on a map" crash. Replaced with distance +
              "Track on Google Maps", which keeps live tracking working with
              no key and no native map. The embedded map can return once a
              new restricted key is set as an EAS env var and the APK rebuilt. */}
          {agentLocation ? (
            <>
              <Text variant="body">
                {(() => {
                  const km = distanceKm(agentLocation, data.address);
                  return km == null
                    ? 'Your delivery partner is on the way.'
                    : km < 0.2
                      ? 'Your delivery partner is almost at your door.'
                      : `Your delivery partner is about ${km < 10 ? km.toFixed(1) : Math.round(km)} km away.`;
                })()}
              </Text>
              <Button
                label="Track on Google Maps"
                variant="secondary"
                onPress={() => openAgentInMaps(agentLocation, data.address)}
              />
            </>
          ) : (
            <Text variant="caption" tone="muted">
              Waiting for the delivery agent's live location…
            </Text>
          )}
        </View>
      )}

      <View style={styles.card}>
        <Text variant="label" style={styles.cardTitle}>Items</Text>
        {data.lines.map((line: OrderView['lines'][number]) => (
          <View key={line.sku} style={styles.line}>
            <View style={styles.lineBody}>
              <Text variant="body" numberOfLines={1}>{line.product_name}</Text>
              <Text variant="caption">
                {line.variant_label} × {line.qty}
              </Text>
            </View>
            <Text variant="priceSmall" style={styles.lineTotal}>
              {formatPaise(line.line_total_paise)}
            </Text>
          </View>
        ))}

        <View style={styles.divider} />
        <View style={styles.line}>
          <Text variant="body">Delivery</Text>
          <Text variant="priceSmall" style={styles.lineTotal}>
            {data.delivery_fee_paise === 0 ? 'Free' : formatPaise(data.delivery_fee_paise)}
          </Text>
        </View>
        <View style={styles.line}>
          <Text variant="label" style={styles.totalLabel}>Total</Text>
          <Text style={styles.totalValue}>{formatPaise(data.total_paise)}</Text>
        </View>
        <Text variant="caption">
          {data.payment.method === 'cod' ? 'Paying on delivery' : 'Paid online'}
        </Text>
      </View>

      <View style={styles.card}>
        <Text variant="label" style={styles.cardTitle}>Delivering to</Text>
        <Text variant="body">{data.address.line1}</Text>
        {data.address.landmark ? (
          <Text variant="caption">{data.address.landmark}</Text>
        ) : null}
        <Text variant="caption">
          {data.address.city} {data.address.pincode}
        </Text>
        {data.can_edit_address && (
          <Pressable onPress={() => router.push(`/order-edit-address?orderId=${data.id}`)}>
            <Text style={styles.editAddressLink}>Edit delivery address</Text>
          </Pressable>
        )}
      </View>

      {/* AAD-BIZ-006: customer cancel removed (see the commented-out
          mutation above).
      {data.can_cancel && (
        <Button
          label="Cancel this order"
          variant="secondary"
          loading={cancel.isPending}
          onPress={() => cancel.mutate('Cancelled from the app')}
        />
      )}

      {cancel.isError && (
        <Text style={styles.error}>
          {cancel.error instanceof Error ? cancel.error.message : 'Could not cancel.'}
        </Text>
      )}
      */}

      <Button
        label="Back to shop"
        variant="ghost"
        onPress={() => router.replace('/')}
      />
    </ScrollView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  hero: { gap: space.xs },
  status: { fontSize: size.xl },
  eta: { color: color.leaf, fontFamily: font.bodyMedium },
  track: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
  },
  trackStep: { alignItems: 'center', gap: space.sm, flex: 1 },
  dot: { width: 12, height: 12, borderRadius: 6, backgroundColor: color.line },
  dotDone: { backgroundColor: color.leaf },
  trackLabel: { fontFamily: font.body, fontSize: size.xs, color: color.muted, textAlign: 'center' },
  trackLabelDone: { color: color.ink, fontFamily: font.bodyMedium },
  card: { backgroundColor: color.card, borderRadius: radius.md, padding: space.lg, gap: space.sm },
  cardTitle: { fontSize: size.base },
  deliveryCode: {
    // AAD-MOB-025: 40px + 10px letterSpacing on a 4-character string is
    // ~250px of glyph-and-gap width before padding — comfortably wider than
    // a smaller Android phone's usable card width, so the last digit (or
    // its trailing letter-spacing) was getting clipped by the card edge.
    // Sized down to something that fits the narrowest phones this app
    // actually needs to support with room to spare; adjustsFontSizeToFit
    // above is just the safety net for whatever's narrower still.
    fontFamily: font.monoBold,
    fontSize: 32,
    letterSpacing: 6,
    color: color.ink,
    textAlign: 'center',
    marginTop: space.xs,
  },
  editAddressLink: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.primary, marginTop: 2 },
  line: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: space.md },
  lineBody: { flex: 1, gap: 2 },
  lineTotal: { color: color.ink },
  divider: { height: 1, backgroundColor: color.line, marginVertical: space.xs },
  totalLabel: { fontSize: size.md },
  totalValue: { fontFamily: font.monoBold, fontSize: size.md, color: color.ink },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
});
