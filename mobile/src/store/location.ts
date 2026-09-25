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
  // AAD-MOB-018: 'located_no_address' is a genuine fifth state, not a flavor
  // of 'error' — the GPS fix succeeded and `latitude`/`longitude` are real;
  // only the reverse-geocode step (street name, pincode) came back empty.
  status: 'idle' | 'locating' | 'found' | 'located_no_address' | 'denied' | 'error';
  label: string | null;          // short line for the header, e.g. "Banjara Hills, Hyderabad"
  line1: string | null;          // fuller line for pre-filling the address form
  pincode: string | null;
  latitude: number | null;
  longitude: number | null;
  request: () => Promise<void>;
}

// AAD-MOB-019: `getCurrentPositionAsync` has no built-in timeout — its own
// doc comment says a fresh fix "may take several seconds" and points at
// `getLastKnownPositionAsync` for exactly this situation. Left unbounded, a
// poor fix (patchy coverage, indoors, a cold GPS chip) left `status` stuck
// at `'locating'` indefinitely, with the checkout button spinning and no way
// out. Raced against this timeout; on either a timeout or a rejection from
// `getCurrentPositionAsync` itself, falls back to whatever position was last
// cached on the device — near-instant, no GPS/network wait — before giving
// up entirely.
const POSITION_TIMEOUT_MS = 10_000;
// A cached last-known fix older than this is more likely to mislead (a
// different city after travel, a stale office location) than to help.
const LAST_KNOWN_MAX_AGE_MS = 10 * 60 * 1000;

function timeout<T>(ms: number): Promise<T> {
  return new Promise((_resolve, reject) => {
    setTimeout(() => reject(new Error('location_timeout')), ms);
  });
}

async function getPositionWithFallback(): Promise<Location.LocationObject | null> {
  try {
    return await Promise.race([
      Location.getCurrentPositionAsync({ accuracy: Location.Accuracy.Balanced }),
      timeout<Location.LocationObject>(POSITION_TIMEOUT_MS),
    ]);
  } catch {
    return Location.getLastKnownPositionAsync({ maxAge: LAST_KNOWN_MAX_AGE_MS });
  }
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

      const position = await getPositionWithFallback();
      if (!position) {
        set({ status: 'error' });
        return;
      }

      // AAD-MOB-018: reverse-geocoding needs network and fails routinely in
      // patchy coverage — exactly where a delivery agent or a customer on a
      // weak connection needs a pin most. Treated the same way whether it
      // throws or just returns nothing: either way the GPS fix above is
      // still good and shouldn't be thrown away with it.
      let place: Location.LocationGeocodedAddress | undefined;
      try {
        [place] = await Location.reverseGeocodeAsync({
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
        });
      } catch {
        place = undefined;
      }

      if (!place) {
        set({
          status: 'located_no_address',
          label: null,
          line1: null,
          pincode: null,
          latitude: position.coords.latitude,
          longitude: position.coords.longitude,
        });
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
