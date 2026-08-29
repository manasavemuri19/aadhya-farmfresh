/**
 * Wire types. These mirror the FastAPI schemas exactly — when the backend
 * changes, this file changes with it, and `tsc` finds every call site.
 */

export type OrderStatus =
  | 'pending_payment'
  | 'confirmed'
  | 'packed'
  | 'out_for_delivery'
  | 'delivered'
  | 'cancelled'
  | 'refunded';

export type PaymentStatus =
  | 'created' | 'authorized' | 'captured' | 'failed' | 'refunded';

export type PaymentMethod = 'online' | 'cod';

export interface Category {
  slug: string;
  name: string;
  sort_order: number;
  is_active: boolean;
}

export interface VariantView {
  sku: string;
  label: string;
  price_paise: number;
  mrp_paise: number | null;
  discount_percent: number;
  in_stock: boolean;
  max_qty: number;
  low_stock: boolean;
}

export interface ProductView {
  id: string;
  slug: string;
  name: string;
  description: string;
  category: string;
  image_url: string;
  prep_minutes: number;
  variants: VariantView[];
}

export interface CatalogResponse {
  categories: Category[];
  products: ProductView[];
  generated_at: string;
}

export interface QuoteLine {
  sku: string;
  product_id: string;
  product_name: string;
  variant_label: string;
  image_url: string;
  qty: number;
  unit_price_paise: number;
  line_total_paise: number;
  adjusted_from_qty: number | null;
  unavailable_reason: string | null;
}

export interface Quote {
  lines: QuoteLine[];
  subtotal_paise: number;
  delivery_fee_paise: number;
  discount_paise: number;
  total_paise: number;
  currency: string;
  free_delivery_threshold_paise: number;
  min_order_paise: number;
  meets_minimum: boolean;
  eta_minutes: number;
  has_adjustments: boolean;
}

export interface Address {
  label: string;
  line1: string;
  line2: string;
  landmark: string;
  city: string;
  pincode: string;
  latitude: number | null;
  longitude: number | null;
}

export interface OrderLine {
  sku: string;
  product_id: string;
  product_name: string;
  variant_label: string;
  image_url: string;
  qty: number;
  unit_price_paise: number;
  line_total_paise: number;
}

export interface StatusEvent {
  status: OrderStatus;
  at: string;
  note: string;
  by: string;
}

export interface PaymentView {
  method: PaymentMethod;
  status: PaymentStatus;
  amount_paise: number;
  provider: string | null;
  provider_order_id: string | null;
  checkout_payload: Record<string, unknown> | null;
}

export interface OrderView {
  id: string;
  order_number: string;
  status: OrderStatus;
  lines: OrderLine[];
  subtotal_paise: number;
  delivery_fee_paise: number;
  discount_paise: number;
  total_paise: number;
  currency: string;
  address: Address;
  notes: string;
  payment: PaymentView;
  eta_minutes: number;
  timeline: StatusEvent[];
  created_at: string;
  updated_at: string;
  can_cancel: boolean;
}

export interface UserProfile {
  id: string;
  phone: string | null;
  email: string | null;
  name: string;
  role: string;
  addresses: Address[];
}

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export interface ApiErrorBody {
  error: { code: string; message: string; details?: Record<string, unknown> };
}

// ---------- admin (staff-only) ----------

export interface AdminVariant {
  sku: string;
  label: string;
  price_paise: number;
  mrp_paise: number | null;
  stock_qty: number;
  is_active: boolean;
}

export interface AdminProduct {
  id: string;
  slug: string;
  name: string;
  category: string;
  is_active: boolean;
  variants: AdminVariant[];
}
