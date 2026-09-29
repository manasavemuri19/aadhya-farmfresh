/**
 * Owner/staff and delivery-agent cash endpoints (AAD-BIZ-004, AAD-BIZ-006,
 * AAD-PAY-021) — kept in their own module rather than added to
 * endpoints.ts/types.ts, so this feature ships as new files plus a few
 * wiring lines instead of edits to the shared API files every screen uses.
 *
 * The backend enforces every role check on its own; these screens being
 * reachable (or not) is never the security boundary.
 */

import { api } from './client';
import type { OrderStatus, OrderView, Page } from './types';

/** Where a delivered COD order's cash is — set only for delivered COD orders. */
export interface CodCash {
  agent_name: string | null;
  amount_paise: number;
  /** true once the owner has recorded receiving it on the Cash tab. */
  settled: boolean;
}

/** An order as owner/staff see it: the customer's view plus COD cash info. */
export type StaffOrder = OrderView & { cod_cash?: CodCash | null };

/** Which slice of orders a list shows. */
export type OrderFilter = 'live' | 'delivered' | 'closed';

const FILTER_STATUSES: Record<OrderFilter, OrderStatus[]> = {
  live: ['confirmed', 'packed', 'out_for_delivery'],
  delivered: ['delivered'],
  closed: ['cancelled', 'refunded'],
};

/** A refund queued at Razorpay that hasn't gone through yet. */
export interface RefundPending {
  order_id: string;
  order_number: string;
  amount_paise: number;
  pending_since: string;
  /** Past the 30-minute alert threshold — the owner has been pushed. */
  stuck: boolean;
}

export interface AgentCashOrder {
  order_id: string;
  order_number: string;
  amount_paise: number;
  collected_at: string;
}

/** One delivery agent's unsettled COD cash — a card on the Cash tab. */
export interface AgentPendingCash {
  agent_id: string;
  agent_name: string | null;
  agent_phone: string | null;
  pending_amount_paise: number;
  orders: AgentCashOrder[];
}

export interface Settlement {
  id: string;
  agent_id: string;
  agent_name: string | null;
  expected_amount_paise: number;
  actual_amount_paise: number;
  discrepancy_paise: number;
  status: 'settled' | 'discrepancy';
  reason: string;
  orders_settled: number;
  created_at: string;
}

export interface MyCash {
  pending_amount_paise: number;
  orders_count: number;
}

export const ownerApi = {
  /**
   * Live orders come oldest-first (a work queue); delivered / cancelled /
   * refunded come newest-first (history), 50 at a time.
   */
  orderList: (filter: OrderFilter, after?: string) => {
    const params = FILTER_STATUSES[filter].map((s) => `status=${s}`);
    if (filter !== 'live') params.push('newest_first=true');
    if (after) params.push(`after=${encodeURIComponent(after)}`);
    return api.get<Page<StaffOrder>>(`/admin/orders?${params.join('&')}`);
  },
  order: (id: string) => api.get<StaffOrder>(`/admin/orders/${id}`),
  /** Owner-only. For a paid online order this also queues the full refund. */
  cancelOrder: (id: string, reason: string) =>
    api.post<OrderView>(
      `/admin/orders/${id}/status`,
      { status: 'cancelled', note: reason },
      { auth: true },
    ),
  /**
   * Owner-only. A goodwill refund on an order that's already been delivered
   * (spoiled milk, wrong item). Queues the full refund to the customer's
   * original payment method; nothing goes back on the shelf.
   */
  refundOrder: (id: string, reason: string) =>
    api.post<OrderView>(
      `/admin/orders/${id}/status`,
      { status: 'refunded', note: reason },
      { auth: true },
    ),
  refundsPending: () => api.get<RefundPending[]>('/admin/refunds/pending'),
  codPending: () => api.get<AgentPendingCash[]>('/admin/cod/pending'),
  settlements: () => api.get<Settlement[]>('/admin/cod/settlements'),
  /**
   * Records cash physically received from an agent. Anything other than the
   * exact expected amount is saved as a discrepancy and needs a reason.
   */
  settle: (agentId: string, actualPaise: number, reason = '') =>
    api.post<Settlement>(
      '/admin/cod/settlements',
      { agent_id: agentId, actual_amount_received_paise: actualPaise, reason },
      { auth: true },
    ),
};

export const agentCashApi = {
  mine: () => api.get<MyCash>('/delivery/cash'),
};
