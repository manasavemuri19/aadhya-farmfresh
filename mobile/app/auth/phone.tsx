import { useState } from 'react';
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, TextInput, View } from 'react-native';
import { useRouter, useLocalSearchParams } from 'expo-router';
import { useMutation } from '@tanstack/react-query';

import { Text } from '../../src/components/Text';
import { Button } from '../../src/components/Button';
import { authApi } from '../../src/api/endpoints';
import { color, font, radius, size, space } from '../../src/theme/tokens';

/**
 * Sign-in start: collect name, number and address together, so a new customer
 * fills everything once. Name and address are held here and saved right after
 * the code is verified (see auth/otp.tsx) — the OTP call itself only needs the
 * phone. Passing them forward as params keeps them off the wire until there's a
 * verified session to attach them to.
 */
export default function PhoneScreen() {
  const router = useRouter();
  const { redirectTo } = useLocalSearchParams<{ redirectTo?: string }>();
  const [name, setName] = useState('');
  const [phone, setPhone] = useState('');
  const [line1, setLine1] = useState('');
  const [pincode, setPincode] = useState('');

  const request = useMutation({
    mutationFn: () => authApi.requestOtp(phone),
    onSuccess: (result) => {
      router.push({
        pathname: '/auth/otp',
        params: {
          phone,
          name: name.trim(),
          line1: line1.trim(),
          pincode: pincode.trim(),
          debugCode: result.debug_code ?? '',
          redirectTo: redirectTo ?? '/',
        },
      });
    },
  });

  const phoneValid = /^[6-9]\d{9}$/.test(phone.replace(/\D/g, ''));
  const nameValid = name.trim().length >= 2;
  const canContinue = phoneValid && nameValid;

  return (
    <KeyboardAvoidingView style={styles.screen} behavior={Platform.OS === 'ios' ? 'padding' : undefined}>
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <Text variant="title">Let's get you set up</Text>
        <Text variant="body">Tell us where to send your order. We'll text a code to confirm your number.</Text>

        <Text variant="label" style={styles.label}>Your name</Text>
        <TextInput
          value={name}
          onChangeText={setName}
          placeholder="e.g. Manasa"
          placeholderTextColor={color.muted}
          autoCapitalize="words"
          style={styles.input}
          accessibilityLabel="Your name"
        />

        <Text variant="label" style={styles.label}>Mobile number</Text>
        <View style={styles.inputRow}>
          <Text style={styles.prefix}>+91</Text>
          <TextInput
            value={phone}
            onChangeText={setPhone}
            placeholder="98765 43210"
            placeholderTextColor={color.muted}
            keyboardType="phone-pad"
            maxLength={10}
            style={styles.inputFlex}
            accessibilityLabel="Mobile number"
          />
        </View>

        <Text variant="label" style={styles.label}>Delivery address <Text style={styles.optional}>(optional now)</Text></Text>
        <TextInput
          value={line1}
          onChangeText={setLine1}
          placeholder="Flat, building and street"
          placeholderTextColor={color.muted}
          style={styles.input}
          accessibilityLabel="Address"
        />
        <TextInput
          value={pincode}
          onChangeText={setPincode}
          placeholder="Pincode"
          placeholderTextColor={color.muted}
          keyboardType="number-pad"
          maxLength={6}
          style={styles.input}
          accessibilityLabel="Pincode"
        />

        {request.isError && (
          <Text style={styles.error}>
            {request.error instanceof Error ? request.error.message : 'Try again.'}
          </Text>
        )}

        <Button
          label="Send code"
          disabled={!canContinue}
          loading={request.isPending}
          onPress={() => request.mutate()}
          style={styles.button}
        />
        <Text variant="caption" style={styles.hint}>
          Waking the kitchen can take a few seconds the first time.
        </Text>
      </ScrollView>
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.surface },
  content: { padding: space.lg, gap: space.sm, paddingBottom: space.xxl },
  label: { marginTop: space.md, fontSize: size.base },
  optional: { fontFamily: font.body, fontSize: size.xs, color: color.muted },
  input: {
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 52,
    fontFamily: font.body,
    fontSize: size.base,
    color: color.ink,
  },
  inputRow: {
    flexDirection: 'row',
    alignItems: 'center',
    backgroundColor: color.card,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.line,
    paddingHorizontal: space.md,
    minHeight: 52,
    gap: space.sm,
  },
  prefix: { fontFamily: font.mono, fontSize: size.md, color: color.muted },
  inputFlex: { flex: 1, fontFamily: font.mono, fontSize: size.md, color: color.ink },
  error: { fontFamily: font.bodyMedium, fontSize: size.sm, color: color.discount, marginTop: space.sm },
  button: { marginTop: space.lg },
  hint: { textAlign: 'center', marginTop: space.sm },
});
