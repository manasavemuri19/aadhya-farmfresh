/**
 * Cart state.
 *
 * The cart holds SKUs and quantities and nothing else — deliberately. It does
 * not store prices, because the server is the only thing allowed to price a
 * cart, and a locally cached price would be a lie the moment the farm changes
 * one. Totals shown to the customer always come from a fresh `/cart/quote`.
 *
 * Persisted to AsyncStorage (not the keychain — a cart is not a secret) so a
 * customer who closes the app mid-shop keeps their basket.
 */

import AsyncStorage from '@react-native-async-storage/async-storage';
import { create } from 'zustand';
import { createJSONStorage, persist } from 'zustand/middleware';

export interface CartItem {
  sku: string;
  qty: number;
  /** Cached for rendering the cart before the quote returns. Never used for money. */
  productName: string;
  variantLabel: string;
  imageUrl: string;
}

interface CartState {
  items: Record<string, CartItem>;
  hydrated: boolean;
  add: (item: Omit<CartItem, 'qty'>, max: number) => void;
  setQty: (sku: string, qty: number, max: number) => void;
  remove: (sku: string) => void;
  clear: () => void;
}

export const useCart = create<CartState>()(
  persist(
    (set) => ({
      items: {},
      hydrated: false,

      add: (item, max) =>
        set((state) => {
          const current = state.items[item.sku]?.qty ?? 0;
          const qty = Math.min(current + 1, Math.max(1, max));
          return { items: { ...state.items, [item.sku]: { ...item, qty } } };
        }),

      setQty: (sku, qty, max) =>
        set((state) => {
          const existing = state.items[sku];
          if (!existing) return state;
          const next = Math.min(Math.max(0, qty), Math.max(1, max));
          if (next === 0) {
            const { [sku]: _removed, ...rest } = state.items;
            return { items: rest };
          }
          return { items: { ...state.items, [sku]: { ...existing, qty: next } } };
        }),

      remove: (sku) =>
        set((state) => {
          const { [sku]: _removed, ...rest } = state.items;
          return { items: rest };
        }),

      clear: () => set({ items: {} }),
    }),
    {
      name: 'aadhya.cart.v1',
      storage: createJSONStorage(() => AsyncStorage),
      partialize: (state) => ({ items: state.items }),
      onRehydrateStorage: () => (state) => {
        if (state) state.hydrated = true;
      },
    },
  ),
);

export function cartLines(items: Record<string, CartItem>): { sku: string; qty: number }[] {
  return Object.values(items).map(({ sku, qty }) => ({ sku, qty }));
}

export function cartCount(items: Record<string, CartItem>): number {
  return Object.values(items).reduce((total, item) => total + item.qty, 0);
}
