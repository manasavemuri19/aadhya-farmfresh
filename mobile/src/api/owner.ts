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
import type { OrderView, Page } from './types';

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
  /** Live orders (confirmed → packed → on the way), oldest first. */
  orderQueue: (after?: string) =>
    api.get<Page<OrderView>>(`/admin/orders${after ? `?after=${encodeURIComponent(after)}` : ''}`),
  order: (id: string) => api.get<OrderView>(`/admin/orders/${id}`),
  /** Owner-only. For a paid online order this also queues the full refund. */
  cancelOrder: (id: string, reason: string) =>
    api.post<OrderView>(
      `/admin/orders/${id}/status`,
      { status: 'cancelled', note: reason },
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
