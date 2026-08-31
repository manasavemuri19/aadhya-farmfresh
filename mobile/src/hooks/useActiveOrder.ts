/**
 * The signed-in customer's own order currently in flight, if any.
 *
 * Shared by the tabs layout (which renders the persistent OrderTrackerBar)
 * and any screen that needs to know whether that bar is on screen right
 * now — e.g. the Order tab, so its own "View cart" bar can leave room for
 * it instead of rendering underneath it. Both call sites reading from the
 * same `['orders']` query key means react-query serves them from one shared
 * cache/poll rather than fetching twice, and the two screens can never
 * disagree about what counts as "in progress."
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

export function useActiveOrder(): OrderView | undefined {
  const role = useSession((s) => s.user?.role);
  const isDeliveryAgent = role === 'delivery_agent';

  const orders = useQuery({
    queryKey: ['orders'],
    queryFn: () => ordersApi.list(),
    refetchInterval: 15_000,
    enabled: !isDeliveryAgent,
  });

  return orders.data?.find((o) => IN_PROGRESS_STATUSES.includes(o.status));
}
