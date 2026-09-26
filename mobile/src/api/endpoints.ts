import { api } from './client';
import type {
  Address, AdminProduct, CatalogResponse, DeliveryOrderView, DeliveryRequestView, OrderStatus,
  OrderView, Page, ProductView, Quote, TokenPair, UserProfile,
} from './types';

export interface CartLineInput { sku: string; qty: number }

// AAD-MOB-006: `api.get` now defaults to `auth: true` — these three are the
// deliberate exception (browsable before sign-in), so they opt out
// explicitly rather than relying on a default that used to mean the same
// thing for every other GET too.
export const catalogApi = {
  get: (category?: string) =>
    api.get<CatalogResponse>(
      `/catalog${category && category !== 'all' ? `?category=${category}` : ''}`,
      false,
    ),
  product: (idOrSlug: string) => api.get<ProductView>(`/catalog/products/${idOrSlug}`, false),
  search: (q: string) => api.get<ProductView[]>(`/catalog/search?q=${encodeURIComponent(q)}`, false),
};

export const cartApi = {
  quote: (lines: CartLineInput[]) => api.post<Quote>('/cart/quote', { lines }),
};

export const authApi = {
  googleSignIn: (idToken: string) =>
    api.post<{ tokens: TokenPair; user: UserProfile }>('/auth/google', { id_token: idToken }),
  me: () => api.get<UserProfile>('/auth/me', true),
  updateName: (name: string) => api.patch<UserProfile>('/auth/me', { name }),
  updatePhone: (phone: string) => api.patch<UserProfile>('/auth/me', { phone }),
  saveAddress: (address: Address) => api.put<void>('/auth/me/addresses', address),
  // One request for all three — see the backend route's own comment for why
  // this replaced three separate sequential calls. Prefer this over the
  // individual methods above wherever more than one field changes at once;
  // they're kept only for call sites that genuinely update just one thing.
  updateProfile: (changes: { name?: string; phone?: string; address?: Address }) =>
    api.patch<UserProfile>('/auth/me', changes),
  // AAD-MOB-022: `previousLabel` is the address's *current* name — the
  // route relabels that row to `address.label` (and updates its other
  // fields at the same time) rather than leaving a duplicate behind, which
  // is what calling `saveAddress` a second time under a new label would do.
  renameAddress: (previousLabel: string, address: Address) =>
    api.patch<void>(`/auth/me/addresses/${encodeURIComponent(previousLabel)}`, address),
  deleteAddress: (label: string) =>
    api.delete<void>(`/auth/me/addresses/${encodeURIComponent(label)}`),
};

export interface CreateOrderInput {
  lines: CartLineInput[];
  address: Address;
  payment_method: 'online' | 'cod';
  notes?: string;
  expected_total_paise?: number;
}

export const ordersApi = {
  create: (input: CreateOrderInput, idempotencyKey: string) =>
    api.post<OrderView>('/orders', input, { auth: true, idempotencyKey }),
  // AAD-API-004: GET /orders now returns a cursor page instead of a bare
  // array, since a customer used to be physically unable to see past their
  // 20 most recent orders. `before` is the previous page's `next_cursor`;
  // omit it for the first page.
  list: (before?: string) =>
    api.get<Page<OrderView>>(`/orders${before ? `?before=${encodeURIComponent(before)}` : ''}`, true),
  get: (id: string) => api.get<OrderView>(`/orders/${id}`, true),
  cancel: (id: string, reason: string) =>
    api.post<OrderView>(`/orders/${id}/cancel`, { reason }, { auth: true }),
  // Only accepted while the order is still can_edit_address — the backend
  // enforces the same window it uses for can_cancel, so this can 403 if the
  // order moved past it between the screen loading and the save.
  updateAddress: (id: string, address: Address) =>
    api.patch<OrderView>(`/orders/${id}/address`, { address }),
};

export const paymentsApi = {
  // Stands in for Razorpay's real checkout SDK + async webhook until his
  // account is live. In production, /payments/verify is a client-side UX
  // check only — the real confirmation always arrives separately, from
  // Razorpay's own servers calling /payments/webhook. A mock provider has no
  // such courier, so this one endpoint produces the same effect a real
  // webhook delivery would, for local testing and demos only.
  mockComplete: (orderId: string, outcome: 'success' | 'failure' = 'success') =>
    api.post<OrderView>('/payments/mock/complete', { order_id: orderId, outcome }, { auth: true }),
  // Real Razorpay: called after the user is redirected back into the app
  // from the Payment Link's hosted checkout page. Query params come exactly
  // as Razorpay sends them on the redirect — see app/payment-callback.tsx.
  confirmLinkCallback: (params: Record<string, string>) =>
    api.get<OrderView>(`/payments/link-callback?${new URLSearchParams(params).toString()}`, true),
};

export const notificationsApi = {
  registerToken: (token: string, platform: 'android' | 'ios' = 'android') =>
    api.post<void>('/notifications/register-token', { token, platform }, { auth: true }),
  // AAD-SEC-032: sign-out used to only ever clear the local keychain —
  // nothing told the server this device should stop receiving the
  // previous account's notifications. Called from session.ts's signOut().
  deregisterToken: (token: string) =>
    api.delete<void>(`/notifications/token?token=${encodeURIComponent(token)}`),
};

export interface SupportTicketCreated { id: string; created_at: string }

export const supportApi = {
  submitTicket: (message: string, contextNodeId: string | null) =>
    api.post<SupportTicketCreated>(
      '/support/tickets',
      { message, context_node_id: contextNodeId },
      { auth: true },
    ),
};

export const adminApi = {
  listProducts: () => api.get<AdminProduct[]>('/admin/products', true),
  setStock: (sku: string, setQty: number) =>
    api.post<{ sku: string; ok: boolean }>('/admin/stock', { sku, set_qty: setQty }, { auth: true }),
  setPrice: (sku: string, pricePaise: number) =>
    api.post<{ sku: string; price_paise: number; ok: boolean }>(
      '/admin/price', { sku, price_paise: pricePaise }, { auth: true },
    ),
  setAvailable: (sku: string, active: boolean) =>
    api.post<{ sku: string; is_active: boolean }>(
      `/admin/products/${sku}/availability?active=${active}`, undefined, { auth: true },
    ),
};

export const deliveryApi = {
  // Paid, unassigned orders within (an expanding) range of the agent's last
  // reported location — see requests.tsx for how that location gets there.
  // AAD-SEC-030: the backend deliberately returns the lean
  // DeliveryRequestView here, not DeliveryOrderView — no address, no notes,
  // no order value, until the agent actually accepts.
  listRequests: () => api.get<DeliveryRequestView[]>('/delivery/requests', true),
  listOngoing: () => api.get<DeliveryOrderView[]>('/delivery/ongoing', true),
  // 409 if someone else's accept landed first — see requests.tsx for how
  // that's surfaced (not a validation error, just "it's gone now").
  accept: (orderId: string) =>
    api.post<DeliveryOrderView>(`/delivery/orders/${orderId}/accept`, undefined, { auth: true }),
  // Backs out of an order this agent already accepted, sending it back to
  // the pool for someone else — only works while it's still just-confirmed
  // (see DeliveryRepository.release on the backend for exactly why).
  release: (orderId: string) =>
    api.post<void>(`/delivery/orders/${orderId}/release`, undefined, { auth: true }),
  // Only 'packed' | 'out_for_delivery' | 'delivered' are accepted — the
  // backend rejects anything else (confirming/cancelling/refunding stay
  // staff-only). The customer's order screen picks this up on its own next
  // poll; nothing needs to be pushed to it from here.
  updateStatus: (orderId: string, status: OrderStatus, note?: string) =>
    api.post<DeliveryOrderView>(`/delivery/orders/${orderId}/status`, { status, note }, { auth: true }),
  reportLocation: (latitude: number, longitude: number) =>
    api.post<void>('/delivery/location', { latitude, longitude }, { auth: true }),
  // AAD-SEC-027: the only way left to reach 'delivered' from this app —
  // 'delivered' is no longer accepted by updateStatus above (the backend
  // rejects it, see _AGENT_ALLOWED_STATUSES). `code` is the 4-digit
  // in-app code the customer reads out; a wrong one comes back as a 409
  // whose message says how many attempts are left, not a generic error.
  verifyDelivery: (orderId: string, code: string) =>
    api.post<DeliveryOrderView>(
      `/delivery/orders/${orderId}/verify-delivery`, { code }, { auth: true },
    ),
};
