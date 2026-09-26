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

// AAD-API-004: the shape GET /orders and the (staff-only, not used from this
// app today) order queue now return instead of a bare array — `has_more`
// says whether there's a next page rather than staying silent about a cut,
// and `next_cursor` is what to send back as `before`/`after` to fetch it.
export interface Page<T> {
  items: T[];
  next_cursor: string | null;
  has_more: boolean;
}

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
  // AAD-QUAL-013: how many of this SKU can be sold right now — always
  // present, not just when adjusted_from_qty is set — so the cart's own
  // quantity stepper can enforce the real ceiling instead of a placeholder
  // that only ever disabled it.
  max_qty: number;
  // 'out_of_stock' (fewer remain than requested) vs 'quantity_limit' (stock
  // is fine; the per-order cap is what bound) — null when qty wasn't
  // reduced at all.
  adjustment_reason: 'out_of_stock' | 'quantity_limit' | null;
}

export interface Quote {
  lines: QuoteLine[];
  subtotal_paise: number;
  delivery_fee_paise: number;
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

/** Only ever present while status is 'out_for_delivery' and an agent is
 * assigned — see the backend's OrderService._agent_location_if_visible. */
export interface AgentLocation {
  latitude: number;
  longitude: number;
  updated_at: string;
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
  // Same window as can_cancel — once an order is packed for pickup, changing
  // its destination needs a person, not a form. See order-edit-address.tsx.
  can_edit_address: boolean;
  delivery_agent_location: AgentLocation | null;
  // AAD-SEC-027: the in-app proof-of-delivery code — set only while the
  // order is genuinely out_for_delivery and the code hasn't expired (see
  // OrderService._delivery_code_if_usable on the backend). Read this out
  // to a delivery agent standing at the door; never shown anywhere on the
  // agent's own side of the app (DeliveryOrderView below has no such
  // field at all).
  delivery_code: string | null;
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

// ---------- delivery agent ----------

/** Leaner than OrderView on purpose — an agent needs where-to-go and
 * what's-in-it, not the customer's payment method or full timeline. */
export interface DeliveryOrderView {
  id: string;
  order_number: string;
  status: OrderStatus;
  address: Address;
  notes: string;
  total_paise: number;
  item_count: number;
  created_at: string;
  delivery_assigned_at: string | null;
  /** null = distance unknown (no coordinates to compare), never "hidden". */
  distance_km: number | null;
}

/** AAD-SEC-030: what GET /delivery/requests actually returns — deliberately
 * leaner than DeliveryOrderView above, so a pending, unaccepted request
 * never reveals the customer's address, notes or order value. Mirrors the
 * backend's DeliveryRequestView field-for-field; this used to be typed (and
 * rendered) as a DeliveryOrderView, which crashed the Requests screen the
 * moment a real pending request came back with no `address` to read
 * `.line1` off of. */
export interface DeliveryRequestView {
  id: string;
  order_number: string;
  item_count: number;
  created_at: string;
  distance_km: number | null;
}
