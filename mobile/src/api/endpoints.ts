import { api } from './client';
import type {
  Address, AdminProduct, CatalogResponse, DeliveryOrderView, OrderStatus, OrderView, ProductView,
  Quote, TokenPair, UserProfile,
} from './types';

export interface CartLineInput { sku: string; qty: number }

export const catalogApi = {
  get: (category?: string) =>
    api.get<CatalogResponse>(`/catalog${category && category !== 'all' ? `?category=${category}` : ''}`),
  product: (idOrSlug: string) => api.get<ProductView>(`/catalog/products/${idOrSlug}`),
  search: (q: string) => api.get<ProductView[]>(`/catalog/search?q=${encodeURIComponent(q)}`),
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
  list: () => api.get<OrderView[]>('/orders', true),
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
  listRequests: () => api.get<DeliveryOrderView[]>('/delivery/requests', true),
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
};
