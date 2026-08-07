import { useEffect, useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, TextInput, View } from 'react-native';
import { useLocalSearchParams, useRouter } from 'expo-router';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { authApi } from '../../src/api/endpoints';
import { tokenStore } from '../../src/store/tokenStore';
import { useSession } from '../../src/store/session';
import { color, font, radius, size, space } from '../../src/theme/tokens';
import type { Address } from '../../src/api/types';

export default function OtpScreen() {
  const router = useRouter();
  const { phone, name, line1, pincode, debugCode, redirectTo } = useLocalSearchParams<{
    phone: string; name: string; line1: string; pincode: string; debugCode: string;
    redirectTo: string;
  }>();
  const setUser = useSession((s) => s.setUser);
  const [code, setCode] = useState('');

  // Convenience for local/staging builds only; absent in production.
  useEffect(() => {
    if (debugCode) setCode(debugCode);
  }, [debugCode]);

  const verify = useMutation({
    mutationFn: () => authApi.verifyOtp(phone, code),
    onSuccess: async (result) => {
      await tokenStore.save(result.tokens);
      let profile = result.user;

      // Save what was collected on the previous screen now that we have a
      // verified session to attach it to. Best-effort: a hiccup here should
      // never block sign-in — the person can always fill it in from Profile.
      try {
        if (name) {
          profile = await authApi.updateName(name);
        }
        if (line1 && /^\d{6}$/.test(pincode ?? '')) {
          const address: Address = {
            label: 'Home', line1, line2: '', landmark: '',
            city: 'Hyderabad', pincode, latitude: null, longitude: null,
          };
          await authApi.saveAddress(address);
          profile = { ...profile, addresses: [address] };
        }
      } catch {
        // Sign-in still succeeds; profile fields are editable later.
      }

      setUser(profile);
      router.replace((redirectTo as string) || '/');
    },
  });

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <View style={styles.content}>
        <Text variant="title">Enter the code</Text>
        <Text variant="body">Sent to {phone}</Text>

        <TextInput
          value={code}
          onChangeText={setCode}
          placeholder="······"
          placeholderTextColor={color.line}
          keyboardType="number-pad"
          maxLength={6}
          autoFocus
          style={styles.input}
          accessibilityLabel="Six digit code"
        />

        {debugCode ? (
          <Text variant="caption">Development build — code filled in automatically</Text>
        ) : null}

        {verify.isError && (
          <Text style={styles.error}>
            {verify.error instanceof Error ? verify.error.message : 'Try again.'}
          </Text>
        )}

        <Button
          label="Verify"
          disabled={code.length < 4}
          loading={verify.isPending}
          onPress={() => verify.mutate()}
        />
        <Button label="Change number" variant="ghost" onPress={() => router.back()} />
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md },
  input: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    minHeight: 64,
    textAlign: 'center',
    fontFamily: font.monoBold,
    fontSize: size.xl,
    letterSpacing: 8,
    color: color.ink,
  },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount },
});
