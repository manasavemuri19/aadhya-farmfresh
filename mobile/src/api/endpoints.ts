import { api } from './client';
import type {
  Address, AdminProduct, CatalogResponse, OrderView, ProductView, Quote, TokenPair, UserProfile,
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
  requestOtp: (phone: string) =>
    api.post<{ sent: boolean; expires_in_seconds: number; resend_after_seconds: number; debug_code: string | null }>(
      '/auth/otp/request', { phone },
    ),
  verifyOtp: (phone: string, code: string) =>
    api.post<{ tokens: TokenPair; user: UserProfile }>('/auth/otp/verify', { phone, code }),
  me: () => api.get<UserProfile>('/auth/me', true),
  updateName: (name: string) => api.patch<UserProfile>('/auth/me', { name }),
  saveAddress: (address: Address) => api.put<void>('/auth/me/addresses', address),
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
