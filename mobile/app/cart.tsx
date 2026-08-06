import { useEffect } from 'react';
import { Image, ScrollView, StyleSheet, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useQuery } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { QtyStepper } from '../src/components/QtyStepper';
import { EmptyState, ErrorState, Loading } from '../src/components/Feedback';
import { cartLines, useCart } from '../src/store/cart';
import { useSession } from '../src/store/session';
import { cartApi } from '../src/api/endpoints';
import { formatPaise } from '../src/lib/money';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Quote } from '../src/api/types';

export default function CartScreen() {
  const router = useRouter();
  const items = useCart((s) => s.items);
  const setQty = useCart((s) => s.setQty);
  const status = useSession((s) => s.status);

  const lines = cartLines(items);

  // The server prices the cart. Re-quoting on every change is what keeps the
  // total honest when the farm adjusts a price or an item sells out.
  const quote = useQuery({
    queryKey: ['quote', lines],
    queryFn: () => cartApi.quote(lines),
    enabled: lines.length > 0,
  });

  // If the quote comes back with adjustments, the local cart is stale — bring
  // it in line so what the customer sees matches what they will be charged.
  useEffect(() => {
    if (!quote.data?.has_adjustments) return;
    for (const line of quote.data.lines) {
      if (line.adjusted_from_qty !== null || line.unavailable_reason) {
        setQty(line.sku, line.qty, Math.max(line.qty, 1));
      }
    }
  }, [quote.data, setQty]);

  if (lines.length === 0) {
    return (
      <EmptyState
        title="Your cart is empty"
        message="Milk, paneer, ghee and pickles are waiting on the shelf."
        actionLabel="Start shopping"
        onAction={() => router.replace('/')}
      />
    );
  }

  if (quote.isPending) return <Loading label="Checking today's prices" />;
  if (quote.isError) {
    return (
      <ErrorState
        message={quote.error instanceof Error ? quote.error.message : 'Try again.'}
        onRetry={() => void quote.refetch()}
      />
    );
  }

  const data: Quote = quote.data;
  const shortfall = data.free_delivery_threshold_paise - data.subtotal_paise;

  return (
    <View style={styles.screen}>
      <ScrollView contentContainerStyle={styles.list}>
        {data.lines.map((line: Quote['lines'][number]) => (
          <View key={line.sku} style={styles.row}>
            {line.image_url ? (
              <Image source={{ uri: line.image_url }} style={styles.thumb} />
            ) : (
              <View style={[styles.thumb, styles.thumbFallback]} />
            )}

            <View style={styles.rowBody}>
              <Text variant="label" numberOfLines={1}>{line.product_name}</Text>
              <Text variant="caption">{line.variant_label}</Text>
              {line.unavailable_reason && (
                <Text style={styles.warning}>
                  {line.unavailable_reason === 'out_of_stock'
                    ? 'Sold out — removed'
                    : 'No longer available'}
                </Text>
              )}
              {line.adjusted_from_qty !== null && !line.unavailable_reason && (
                <Text style={styles.warning}>
                  Only {line.qty} left — quantity updated
                </Text>
              )}
            </View>

            <View style={styles.rowEnd}>
              <Text variant="price">{formatPaise(line.line_total_paise)}</Text>
              <QtyStepper
                qty={line.qty}
                max={Math.max(line.qty, 1)}
                onChange={(qty) => setQty(line.sku, qty, Math.max(line.qty, 1))}
                compact
              />
            </View>
          </View>
        ))}

        <View style={styles.summary}>
          <SummaryRow label="Items" value={formatPaise(data.subtotal_paise)} />
          <SummaryRow
            label="Delivery"
            value={data.delivery_fee_paise === 0 ? 'Free' : formatPaise(data.delivery_fee_paise)}
          />
          {shortfall > 0 && (
            <Text style={styles.nudge}>
              Add {formatPaise(shortfall)} more for free delivery
            </Text>
          )}
          <View style={styles.divider} />
          <View style={styles.totalRow}>
            <Text variant="label" style={styles.totalLabel}>Total</Text>
            <Text style={styles.totalValue}>{formatPaise(data.total_paise)}</Text>
          </View>
          <Text variant="caption" style={styles.eta}>
            Arriving in about {data.eta_minutes} minutes
          </Text>
        </View>
      </ScrollView>

      <View style={styles.footer}>
        {!data.meets_minimum && (
          <Text style={styles.warning}>
            Minimum order is {formatPaise(data.min_order_paise)}
          </Text>
        )}
        <Button
          label={status === 'signed_in' ? 'Choose address' : 'Sign in to continue'}
          disabled={!data.meets_minimum}
          onPress={() =>
            router.push(status === 'signed_in' ? '/checkout' : '/auth/phone')
          }
        />
      </View>
    </View>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <View style={styles.summaryRow}>
      <Text variant="body">{label}</Text>
      <Text variant="priceSmall" style={styles.summaryValue}>{value}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  list: { padding: space.lg, paddingBottom: space.xl },
  row: {
    flexDirection: 'row',
    gap: space.md,
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    marginBottom: space.sm,
    alignItems: 'center',
  },
  thumb: { width: 52, height: 52, borderRadius: radius.sm, backgroundColor: color.surface },
  thumbFallback: { backgroundColor: color.leafSoft },
  rowBody: { flex: 1, gap: 2 },
  rowEnd: { alignItems: 'flex-end', gap: space.sm },
  warning: { fontFamily: font.bodyMedium, fontSize: size.xs, color: color.lowStock },
  summary: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.lg,
    marginTop: space.md,
    gap: space.sm,
  },
  summaryRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  summaryValue: { color: color.ink },
  nudge: { fontFamily: font.bodyMedium, fontSize: size.xs, color: color.leaf },
  divider: { height: 1, backgroundColor: color.line, marginVertical: space.xs },
  totalRow: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center' },
  totalLabel: { fontSize: size.md },
  totalValue: { fontFamily: font.monoBold, fontSize: size.lg, color: color.ink },
  eta: { color: color.leaf },
  footer: {
    padding: space.lg,
    borderTopWidth: 1,
    borderTopColor: color.line,
    backgroundColor: color.card,
    gap: space.sm,
  },
});
