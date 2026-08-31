import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, Pressable, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { ErrorState, Loading } from '../src/components/Feedback';
import { ordersApi } from '../src/api/endpoints';
import { useLocationStore } from '../src/store/location';
import { color, font, radius, size, space } from '../src/theme/tokens';
import type { Address, OrderView } from '../src/api/types';

/**
 * A flat route (not nested under order/[id]) so it can be pushed from the
 * order screen with a plain `?orderId=` param, the same shape as the rest
 * of this app's one-off screens (edit-details, payment-callback).
 *
 * Editable only while the order is still can_edit_address (see
 * order_service.CUSTOMER_CANCELLABLE on the backend) — once it's packed for
 * pickup, the farm has already dispatched against the old address, so this
 * screen shows a blocked state instead of a form that would 403 on submit.
 */
export default function OrderEditAddressScreen() {
  const { orderId } = useLocalSearchParams<{ orderId: string }>();
  const router = useRouter();
  const queryClient = useQueryClient();
  const location = useLocationStore();

  const order = useQuery({
    queryKey: ['order', orderId],
    queryFn: () => ordersApi.get(orderId),
  });

  const [line1, setLine1] = useState('');
  const [landmark, setLandmark] = useState('');
  const [pincode, setPincode] = useState('');
  const [prefilled, setPrefilled] = useState(false);
  // Only replaced when the person edits the address text or taps "use my
  // current location" — otherwise the order's existing coordinates (if any)
  // travel through unchanged.
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(null);

  useEffect(() => {
    if (prefilled || !order.data) return;
    setLine1(order.data.address.line1);
    setLandmark(order.data.address.landmark);
    setPincode(order.data.address.pincode);
    if (order.data.address.latitude != null && order.data.address.longitude != null) {
      setCoords({ latitude: order.data.address.latitude, longitude: order.data.address.longitude });
    }
    setPrefilled(true);
  }, [order.data, prefilled]);

  const useCurrentLocation = () => {
    if (location.status === 'found' && location.line1) {
      setLine1(location.line1);
      if (location.pincode) setPincode(location.pincode);
      if (location.latitude != null && location.longitude != null) {
        setCoords({ latitude: location.latitude, longitude: location.longitude });
      }
    } else {
      void location.request();
    }
  };

  const save = useMutation({
    mutationFn: () => {
      const current = order.data as OrderView;
      const address: Address = {
        label: current.address.label,
        line1: line1.trim(),
        line2: current.address.line2,
        landmark: landmark.trim(),
        city: current.address.city,
        pincode: pincode.trim(),
        latitude: coords?.latitude ?? null,
        longitude: coords?.longitude ?? null,
      };
      return ordersApi.updateAddress(orderId, address);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['order', orderId] });
      void queryClient.invalidateQueries({ queryKey: ['orders'] });
      router.back();
    },
  });

  if (order.isPending) return <Loading />;
  if (order.isError) {
    return (
      <ErrorState
        message={order.error instanceof Error ? order.error.message : 'Try again.'}
        onRetry={() => void order.refetch()}
      />
    );
  }

  if (!order.data.can_edit_address) {
    return (
      <View style={styles.blocked}>
        <Text variant="title">Too late to change this</Text>
        <Text variant="body" style={styles.blockedBody}>
          Order {order.data.order_number} is already on its way to being delivered, so the address
          can no longer be changed here. Message us from Help & Support with the order number and
          the correct address and we'll try to catch it.
        </Text>
        <Button label="Back to order" variant="secondary" onPress={() => router.back()} />
      </View>
    );
  }

  const addressValid = line1.trim().length >= 4 && /^\d{6}$/.test(pincode.trim());

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text variant="title">Deliver order {order.data.order_number} to</Text>

        <Pressable
          onPress={useCurrentLocation}
          accessibilityRole="button"
          style={({ pressed }) => [styles.locationButton, pressed && styles.locationButtonPressed]}
        >
          <Text style={styles.locationButtonText}>
            {location.status === 'locating' ? '📍 Finding your location…' : '📍 Use my current location'}
          </Text>
        </Pressable>

        <Field
          label="Flat, building and street"
          value={line1}
          onChangeText={(t) => { setLine1(t); setCoords(null); }}
          placeholder="12-3-45, Rose Villa, Banjara Hills"
          autoComplete="street-address"
        />
        <Field
          label="Landmark (optional)"
          value={landmark}
          onChangeText={setLandmark}
          placeholder="Opposite the temple"
        />
        <Field
          label="Pincode"
          value={pincode}
          onChangeText={(t) => { setPincode(t); setCoords(null); }}
          placeholder="500034"
          keyboardType="number-pad"
          maxLength={6}
        />

        {save.isError && (
          <View style={styles.errorBox}>
            <Text style={styles.errorText}>
              {save.error instanceof Error ? save.error.message : 'Could not update the address.'}
            </Text>
          </View>
        )}
      </ScrollView>

      <View style={styles.footer}>
        <Button
          label="Save new address"
          disabled={!addressValid}
          loading={save.isPending}
          onPress={() => save.mutate()}
        />
      </View>
    </KeyboardAvoidingView>
  );
}

function Field({
  label, ...props
}: { label: string } & React.ComponentProps<typeof TextInput>) {
  return (
    <View style={styles.field}>
      <Text variant="caption">{label}</Text>
      <TextInput
        {...props}
        style={styles.input}
        placeholderTextColor={color.muted}
        accessibilityLabel={label}
      />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md, paddingBottom: space.xxl },
  blocked: { flex: 1, backgroundColor: color.surface, padding: space.lg, gap: space.md, justifyContent: 'center' },
  blockedBody: { color: color.muted },
  locationButton: {
    backgroundColor: color.leafSoft,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.leaf,
    paddingVertical: space.sm,
    paddingHorizontal: space.md,
    alignItems: 'center',
  },
  locationButtonPressed: { opacity: 0.8 },
  locationButtonText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.leaf },
  field: { gap: space.xs },
  input: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 50,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
  },
  errorBox: {
    backgroundColor: color.discountSoft,
    borderRadius: radius.md,
    padding: space.md,
    gap: space.xs,
  },
  errorText: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
  footer: {
    padding: space.lg,
    borderTopWidth: 1,
    borderTopColor: color.line,
    backgroundColor: color.card,
  },
});
