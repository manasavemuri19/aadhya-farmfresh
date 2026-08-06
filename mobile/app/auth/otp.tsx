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

export default function OtpScreen() {
  const router = useRouter();
  const { phone, debugCode } = useLocalSearchParams<{ phone: string; debugCode: string }>();
  const setUser = useSession((s) => s.setUser);
  const [code, setCode] = useState('');

  // Convenience for local development only; absent in any real build.
  useEffect(() => {
    if (debugCode) setCode(debugCode);
  }, [debugCode]);

  const verify = useMutation({
    mutationFn: () => authApi.verifyOtp(phone, code),
    onSuccess: async (result) => {
      await tokenStore.save(result.tokens);
      setUser(result.user);
      router.replace('/checkout');
    },
  });

  return (
    <KeyboardAvoidingView
      style={styles.screen}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
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
