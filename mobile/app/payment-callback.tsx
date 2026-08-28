import { useEffect, useRef, useState } from 'react';
import { StyleSheet, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';

import { Text } from '../src/components/Text';
import { Button } from '../src/components/Button';
import { Loading } from '../src/components/Feedback';
import { paymentsApi } from '../src/api/endpoints';
import { color, size, space } from '../src/theme/tokens';

/**
 * The far end of the redirect Razorpay sends the browser to after a Payment
 * Link is paid — see RazorpayProvider.create_order (backend) for why this
 * exists as a URL-based redirect rather than an in-app SDK callback.
 *
 * Registered automatically by expo-router from app.config.js's `scheme`:
 * `aadhya://payment-callback?...` opens this screen directly, query params
 * intact, exactly as Razorpay's GET redirect delivers them.
 */
export default function PaymentCallbackScreen() {
  const router = useRouter();
  const params = useLocalSearchParams<{
    razorpay_payment_id?: string;
    razorpay_payment_link_id?: string;
    razorpay_payment_link_reference_id?: string;
    razorpay_payment_link_status?: string;
    razorpay_signature?: string;
  }>();
  const [status, setStatus] = useState<'checking' | 'ok' | 'error'>('checking');
  const [message, setMessage] = useState('');
  const attempted = useRef(false);

  useEffect(() => {
    if (attempted.current) return;
    attempted.current = true;

    const orderId = params.razorpay_payment_link_reference_id;
    if (!orderId || !params.razorpay_signature) {
      setStatus('error');
      setMessage('This payment link is missing information and could not be confirmed.');
      return;
    }

    paymentsApi
      .confirmLinkCallback({
        razorpay_payment_id: params.razorpay_payment_id ?? '',
        razorpay_payment_link_id: params.razorpay_payment_link_id ?? '',
        razorpay_payment_link_reference_id: orderId,
        razorpay_payment_link_status: params.razorpay_payment_link_status ?? '',
        razorpay_signature: params.razorpay_signature,
      })
      .then((order) => {
        if (order.status === 'confirmed') {
          router.replace(`/order/${orderId}`);
        } else {
          setStatus('error');
          setMessage('That payment did not go through.');
        }
      })
      .catch((err) => {
        setStatus('error');
        setMessage(err instanceof Error ? err.message : 'Could not confirm this payment.');
      });
  }, [params, router]);

  if (status === 'checking') return <Loading label="Confirming your payment" />;

  return (
    <View style={styles.screen}>
      <Text variant="title">Payment not confirmed</Text>
      <Text variant="body" style={styles.message}>{message}</Text>
      <Button label="Back to orders" onPress={() => router.replace('/orders')} />
    </View>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface, padding: space.lg, gap: space.md, justifyContent: 'center', alignItems: 'center' },
  message: { textAlign: 'center', fontSize: size.base },
});
