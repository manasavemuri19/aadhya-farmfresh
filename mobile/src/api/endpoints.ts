import { api } from './client';
import type {
  Address, CatalogResponse, OrderView, ProductView, Quote, TokenPair, UserProfile,
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
