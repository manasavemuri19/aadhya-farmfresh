import { useState } from 'react';
import { Alert, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useSafeAreaInsets } from 'react-native-safe-area-context';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { ErrorState, Loading } from '../../src/components/Feedback';
import { ownerApi, type AgentPendingCash, type Settlement } from '../../src/api/owner';
import { formatPaise } from '../../src/lib/money';
import { shortDateTime } from '../../src/lib/time';
import { color, font, radius, size, space } from '../../src/theme/tokens';

/**
 * AAD-BIZ-004: the owner's COD cash screen — owner-only (tab hidden for
 * everyone else; the backend enforces it regardless).
 *
 * Cash is "with" an agent from the moment a COD delivery is verified with
 * the customer's code, until the owner records receiving it here. The usual
 * case is one tap — "Mark ₹X received" settles everything that agent holds.
 * If the cash handed over doesn't match, "Received a different amount"
 * records what actually came in plus a reason, saved as a discrepancy
 * rather than silently marked settled. Agents see the same running total on
 * their own Requests screen.
 */
export default function CashScreen() {
  const insets = useSafeAreaInsets();
  const pending = useQuery({
    queryKey: ['owner', 'cod-pending'],
    queryFn: () => ownerApi.codPending(),
    refetchInterval: 60_000,
  });
  const history = useQuery({
    queryKey: ['owner', 'cod-settlements'],
    queryFn: () => ownerApi.settlements(),
  });

  if (pending.isPending) return <Loading label="Loading cash" />;
  if (pending.isError) return <ErrorState error={pending.error} onRetry={() => void pending.refetch()} />;

  const cards = pending.data;
  const total = cards.reduce((n, c) => n + c.pending_amount_paise, 0);

  return (
    <ScrollView
      style={styles.screen}
      contentContainerStyle={[styles.content, { paddingTop: insets.top + space.lg }]}
      keyboardShouldPersistTaps="handled"
    >
      <Text variant="display" style={styles.heading}>Cash</Text>

      <View style={styles.summary}>
        <Text variant="caption">Cash with delivery partners</Text>
        <Text style={styles.summaryAmount}>{formatPaise(total)}</Text>
      </View>

      {cards.length === 0 ? (
        <Text variant="body" style={styles.empty}>
          No cash waiting to be collected. COD orders appear here once they're delivered.
        </Text>
      ) : (
        cards.map((card) => <AgentCard key={card.agent_id} card={card} />)
      )}

      <Text variant="label" style={styles.sectionLabel}>Recent settlements</Text>
      {history.isPending ? (
        <Text variant="caption">Loading…</Text>
      ) : (history.data ?? []).length === 0 ? (
        <Text variant="caption">Nothing settled yet.</Text>
      ) : (
        (history.data ?? []).map((s) => <SettlementRow key={s.id} settlement={s} />)
      )}
    </ScrollView>
  );
}

function AgentCard({ card }: { card: AgentPendingCash }) {
  const queryClient = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [custom, setCustom] = useState(false);
  const [amount, setAmount] = useState('');
  const [reason, setReason] = useState('');
  const name = card.agent_name || card.agent_phone || 'Delivery partner';

  const settle = useMutation({
    mutationFn: (args: { paise: number; reason: string }) =>
      ownerApi.settle(card.agent_id, args.paise, args.reason),
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ['owner', 'cod-pending'] });
      void queryClient.invalidateQueries({ queryKey: ['owner', 'cod-settlements'] });
      setCustom(false);
      setAmount('');
      setReason('');
      if (result.status === 'discrepancy') {
        Alert.alert(
          'Recorded with a difference',
          `Expected ${formatPaise(result.expected_amount_paise)}, received ${formatPaise(result.actual_amount_paise)}. Saved with your reason.`,
        );
      }
    },
  });

  const confirmExact = () => {
    Alert.alert(
      `Received ${formatPaise(card.pending_amount_paise)}?`,
      `Confirm you've collected ${formatPaise(card.pending_amount_paise)} in cash from ${name} for ${card.orders.length} order${card.orders.length === 1 ? '' : 's'}.`,
      [
        { text: 'Not yet', style: 'cancel' },
        { text: 'Yes, received', onPress: () => settle.mutate({ paise: card.pending_amount_paise, reason: '' }) },
      ],
    );
  };

  const customRupees = parseFloat(amount);
  const customValid = !Number.isNaN(customRupees) && customRupees >= 0 && reason.trim().length > 0;

  return (
    <View style={styles.card}>
      <Pressable onPress={() => setExpanded((e) => !e)} accessibilityRole="button">
        <View style={styles.rowTop}>
          <View style={styles.rowBody}>
            <Text style={styles.agentName}>{name}</Text>
            {card.agent_name && card.agent_phone ? <Text variant="caption">{card.agent_phone}</Text> : null}
          </View>
          <Text style={styles.amount}>{formatPaise(card.pending_amount_paise)}</Text>
        </View>
        <Text variant="caption">
          {card.orders.length} order{card.orders.length === 1 ? '' : 's'} · {expanded ? 'Hide' : 'Show'} orders
        </Text>
      </Pressable>

      {expanded &&
        card.orders.map((o) => (
          <View key={o.order_id} style={styles.orderLine}>
            <Text variant="caption">#{o.order_number} · {shortDateTime(o.collected_at)}</Text>
            <Text variant="caption" style={styles.ink}>{formatPaise(o.amount_paise)}</Text>
          </View>
        ))}

      {settle.isError && (
        <Text style={styles.error}>
          {settle.error instanceof Error ? settle.error.message : 'Could not record that. Try again.'}
        </Text>
      )}

      {!custom ? (
        <>
          <Button
            label={`Mark ${formatPaise(card.pending_amount_paise)} received`}
            loading={settle.isPending}
            onPress={confirmExact}
          />
          <Text style={styles.link} onPress={settle.isPending ? undefined : () => setCustom(true)}>
            Received a different amount
          </Text>
        </>
      ) : (
        <>
          <TextInput
            value={amount}
            onChangeText={(t) => setAmount(t.replace(/[^0-9.]/g, ''))}
            placeholder="Amount received (₹)"
            keyboardType="decimal-pad"
            style={styles.input}
            placeholderTextColor={color.muted}
          />
          <TextInput
            value={reason}
            onChangeText={setReason}
            placeholder="Reason for the difference"
            maxLength={300}
            style={styles.input}
            placeholderTextColor={color.muted}
          />
          <Button
            label="Record amount"
            loading={settle.isPending}
            disabled={!customValid}
            onPress={() => settle.mutate({ paise: Math.round(customRupees * 100), reason: reason.trim() })}
          />
          <Text
            style={styles.link}
            onPress={settle.isPending ? undefined : () => { setCustom(false); setAmount(''); setReason(''); }}
          >
            Cancel
          </Text>
        </>
      )}
    </View>
  );
}

function SettlementRow({ settlement: s }: { settlement: Settlement }) {
  const diff = s.discrepancy_paise;
  return (
    <View style={styles.historyRow}>
      <View style={styles.rowBody}>
        <Text variant="body">{s.agent_name || 'Delivery partner'}</Text>
        <Text variant="caption">
          {shortDateTime(s.created_at)} · {s.orders_settled} order{s.orders_settled === 1 ? '' : 's'}
        </Text>
        {diff !== 0 && (
          <Text variant="caption" style={styles.errorText}>
            {diff < 0 ? `Short by ${formatPaise(-diff)}` : `Over by ${formatPaise(diff)}`}
            {s.reason ? ` — ${s.reason}` : ''}
          </Text>
        )}
      </View>
      <Text style={[styles.amount, diff === 0 ? styles.goodText : styles.errorText]}>
        {formatPaise(s.actual_amount_paise)}
      </Text>
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { paddingHorizontal: space.lg, paddingBottom: space.xxl, gap: space.sm },
  heading: { marginBottom: space.sm },
  summary: {
    backgroundColor: color.primarySoft,
    borderRadius: radius.md,
    padding: space.lg,
    gap: 2,
    marginBottom: space.sm,
  },
  summaryAmount: { fontFamily: font.monoBold, fontSize: size.xl, color: color.ink },
  empty: { marginVertical: space.md },
  sectionLabel: { marginTop: space.lg, fontSize: size.base },
  card: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
    borderWidth: 1,
    borderColor: color.line,
    gap: space.sm,
  },
  rowTop: { flexDirection: 'row', justifyContent: 'space-between', alignItems: 'center', gap: space.sm },
  rowBody: { flex: 1, gap: 2 },
  agentName: { fontFamily: font.bodyBold, fontSize: size.base, color: color.ink },
  amount: { fontFamily: font.monoBold, fontSize: size.md, color: color.ink },
  orderLine: { flexDirection: 'row', justifyContent: 'space-between' },
  ink: { color: color.ink },
  link: { textAlign: 'center', color: color.primary, fontFamily: font.bodyMedium, fontSize: size.sm, paddingVertical: 4 },
  input: {
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 48,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
  },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  errorText: { color: color.discount },
  goodText: { color: color.leaf },
  historyRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    backgroundColor: color.card,
    borderRadius: radius.md,
    padding: space.md,
  },
});
