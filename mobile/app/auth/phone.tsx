import { useState } from 'react';
import { KeyboardAvoidingView, Platform, StyleSheet, TextInput, View } from 'react-native';
import { useRouter } from 'expo-router';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { authApi } from '../../src/api/endpoints';
import { color, font, radius, size, space } from '../../src/theme/tokens';

export default function PhoneScreen() {
  const router = useRouter();
  const [phone, setPhone] = useState('');

  const request = useMutation({
    mutationFn: () => authApi.requestOtp(phone),
    onSuccess: (result) => {
      router.push({
        pathname: '/auth/otp',
        // The debug code is only ever present in local and staging builds —
        // the server refuses to boot production with the echo enabled.
        params: { phone, debugCode: result.debug_code ?? '' },
      });
    },
  });

  const valid = /^[6-9]\d{9}$/.test(phone.replace(/\D/g, ''));

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <View style={styles.content}>
        <Text variant="title">What is your mobile number?</Text>
        <Text variant="body">
          We will text you a code. No password to remember.
        </Text>

        <View style={styles.inputRow}>
          <Text style={styles.prefix}>+91</Text>
          <TextInput
            value={phone}
            onChangeText={setPhone}
            placeholder="98765 43210"
            placeholderTextColor={color.muted}
            keyboardType="phone-pad"
            maxLength={10}
            autoFocus
            style={styles.input}
            accessibilityLabel="Mobile number"
          />
        </View>

        {request.isError && (
          <Text style={styles.error}>
            {request.error instanceof Error ? request.error.message : 'Try again.'}
          </Text>
        )}

        <Button
          label="Send code"
          disabled={!valid}
          loading={request.isPending}
          onPress={() => request.mutate()}
        />
      </View>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.md },
  inputRow: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 56,
    gap: space.sm,
  },
  prefix: { fontFamily: font.mono, fontSize: size.md, color: color.muted },
  input: { flex: 1, fontFamily: font.mono, fontSize: size.md, color: color.ink },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.chilli },
});
