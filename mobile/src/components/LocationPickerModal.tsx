import { useCallback, useState } from 'react';
import { Modal, StyleSheet, View } from 'react-native';
import MapView, { PROVIDER_GOOGLE, type Region } from 'react-native-maps';
import * as Location from 'expo-location';

import { Text } from './Text';
import { Button } from './Button';
import { color, space } from '../theme/tokens';

export interface PickedLocation {
  latitude: number;
  longitude: number;
  /** null when reverse geocoding found nothing (or failed) — the calling
   *  screen keeps whatever address text it already had rather than clearing it. */
  line1: string | null;
  pincode: string | null;
}

// Hyderabad city centre — every address screen in this app defaults `city`
// to Hyderabad, so this is the sensible starting point when there's no
// address typed yet and device location hasn't resolved either.
const FALLBACK_REGION: Region = {
  latitude: 17.385,
  longitude: 78.4867,
  latitudeDelta: 0.05,
  longitudeDelta: 0.05,
};

interface Props {
  visible: boolean;
  /** Centres the map here on open — the address form's current coordinates
   *  if it has any, otherwise the caller can pass device location, otherwise
   *  null falls back to the Hyderabad default above. */
  initialCoords: { latitude: number; longitude: number } | null;
  onConfirm: (result: PickedLocation) => void;
  onClose: () => void;
}

/**
 * A pin fixed at the centre of the screen, with the map free to drag under
 * it — the standard "drag the map, not the pin" pattern. Reverse geocoding
 * runs once, on Confirm, rather than on every drag frame: one network call
 * instead of dozens, and the person can see exactly which point they've
 * landed on before it resolves to a street address.
 *
 * Rendered as a Modal rather than a routed screen so it can drop into any
 * address form as-is — including CompleteProfileScreen, which is shown
 * before the app's Stack navigator exists and so has nowhere to route to.
 */
export function LocationPickerModal({ visible, initialCoords, onConfirm, onClose }: Props) {
  const initialRegion: Region = initialCoords
    ? { latitude: initialCoords.latitude, longitude: initialCoords.longitude, latitudeDelta: 0.01, longitudeDelta: 0.01 }
    : FALLBACK_REGION;
  const [center, setCenter] = useState({ latitude: initialRegion.latitude, longitude: initialRegion.longitude });
  const [resolving, setResolving] = useState(false);

  const onRegionChangeComplete = useCallback((region: Region) => {
    setCenter({ latitude: region.latitude, longitude: region.longitude });
  }, []);

  const confirm = async () => {
    setResolving(true);
    try {
      const [place] = await Location.reverseGeocodeAsync(center);
      const full = place
        ? [place.streetNumber, place.street, place.district].filter(Boolean).join(' ')
        : null;
      onConfirm({
        latitude: center.latitude,
        longitude: center.longitude,
        line1: full || null,
        pincode: place?.postalCode ?? null,
      });
    } catch {
      onConfirm({ latitude: center.latitude, longitude: center.longitude, line1: null, pincode: null });
    } finally {
      setResolving(false);
    }
  };

  return (
    <Modal visible={visible} animationType="slide" onRequestClose={onClose}>
      <View style={styles.screen}>
        <MapView
          provider={PROVIDER_GOOGLE}
          style={styles.map}
          initialRegion={initialRegion}
          onRegionChangeComplete={onRegionChangeComplete}
        />
        <View style={styles.pin} pointerEvents="none">
          <Text style={styles.pinGlyph}>📍</Text>
        </View>

        <View style={styles.footer}>
          <Text variant="caption" style={styles.hint}>
            Drag the map so the pin marks your building
          </Text>
          <Button label="Confirm this location" loading={resolving} onPress={() => void confirm()} />
          <Text style={styles.cancelLink} onPress={onClose}>
            Cancel
          </Text>
        </View>
      </View>
    </Modal>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  map: { flex: 1 },
  pin: {
    position: 'absolute',
    top: '50%',
    left: '50%',
    marginLeft: -18,
    marginTop: -36,
  },
  pinGlyph: { fontSize: 36 },
  footer: {
    padding: space.lg,
    gap: space.sm,
    borderTopWidth: 1,
    borderTopColor: color.line,
    backgroundColor: color.card,
  },
  hint: { textAlign: 'center' },
  cancelLink: { textAlign: 'center', color: color.muted, marginTop: 4 },
});
