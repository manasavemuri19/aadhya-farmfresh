/**
 * Device location — fetched on the shop screen, shown under the wordmark,
 * and reused as the checkout address default.
 *
 * Location is asked for at the moment it's useful (opening the shop), not at
 * app launch — a permission prompt before someone has even seen the app
 * reads as invasive. If permission is denied, the app keeps working; there's
 * just no address line under the header, and checkout falls back to typing
 * one in by hand.
 */

import { create } from 'zustand';
import * as Location from 'expo-location';

interface LocationState {
  status: 'idle' | 'locating' | 'found' | 'denied' | 'error';
  label: string | null;          // short line for the header, e.g. "Banjara Hills, Hyderabad"
  line1: string | null;          // fuller line for pre-filling the address form
  pincode: string | null;
  latitude: number | null;
  longitude: number | null;
  request: () => Promise<void>;
}

export const useLocationStore = create<LocationState>((set, get) => ({
  status: 'idle',
  label: null,
  line1: null,
  pincode: null,
  latitude: null,
  longitude: null,

  request: async () => {
    if (get().status === 'locating') return;
    set({ status: 'locating' });

    try {
      const { status } = await Location.requestForegroundPermissionsAsync();
      if (status !== 'granted') {
        set({ status: 'denied' });
        return;
      }

      const position = await Location.getCurrentPositionAsync({
        accuracy: Location.Accuracy.Balanced,
      });

      const [place] = await Location.reverseGeocodeAsync({
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
      });

      if (!place) {
        set({ status: 'error' });
        return;
      }

      // Short label for the header: neighbourhood + city, whatever is present.
      const short = [place.district ?? place.subregion ?? place.street, place.city]
        .filter(Boolean)
        .join(', ');

      const full = [place.streetNumber, place.street, place.district]
        .filter(Boolean)
        .join(' ');

      set({
        status: 'found',
        label: short || place.city || 'Current location',
        line1: full || short || null,
        pincode: place.postalCode ?? null,
        latitude: position.coords.latitude,
        longitude: position.coords.longitude,
      });
    } catch {
      set({ status: 'error' });
    }
  },
}));
