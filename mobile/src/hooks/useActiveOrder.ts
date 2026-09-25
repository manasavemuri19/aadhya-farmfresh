/**
 * All of the signed-in customer's own orders currently in flight, if any —
 * plural, because a customer can have more than one order in progress at
 * once (e.g. one just placed while an earlier one is already out for
 * delivery), and the tracker UI needs to stack one bar per order rather
 * than showing only the single most-recent one.
 *
 * Shared by the tabs layout (which renders a persistent OrderTrackerBar per
 * active order) and any screen that needs to know how many of those bars
 * are on screen right now — e.g. the Order tab, so its own "View cart" bar
 * can leave room for all of them instead of rendering underneath the
 * stack. Both call sites reading from the same `['orders']` query key means
 * react-query serves them from one shared cache/poll rather than fetching
 * twice, and the two screens can never disagree about what counts as
 * "in progress."
 *
 * Polling, not push — see OrderTrackerBar for why. Disabled for a delivery
 * agent, who has no placed orders of their own to track.
 */

import { useQuery } from '@tanstack/react-query';

import { ordersApi } from '../api/endpoints';
import { useSession } from '../store/session';
import type { OrderStatus, OrderView } from '../api/types';

// Placed once an order is paid/confirmed, until it's actually in the
// customer's hands — this is the window the tracker bar should cover.
const IN_PROGRESS_STATUSES: readonly OrderStatus[] = ['confirmed', 'packed', 'out_for_delivery'];

export function useActiveOrders(): OrderView[] {
  const role = useSession((s) => s.user?.role);
  const isDeliveryAgent = role === 'delivery_agent';

  const orders = useQuery({
    queryKey: ['orders'],
    queryFn: () => ordersApi.list(),
    refetchInterval: 15_000,
    enabled: !isDeliveryAgent,
  });

  // AAD-API-004: list() now returns a cursor page. Only the first page is
  // ever checked here, which is deliberate, not an oversight — an order
  // that's still in flight (confirmed/packed/out_for_delivery) is by
  // definition recent, so it's vanishingly unlikely to have already been
  // pushed past the first 20 by newer orders. Older, unpaginated history is
  // exactly what a completed/cancelled/refunded order becomes, and this
  // hook was never meant to cover that — see orders.tsx for full history.
  return orders.data?.items.filter((o) => IN_PROGRESS_STATUSES.includes(o.status)) ?? [];
}
