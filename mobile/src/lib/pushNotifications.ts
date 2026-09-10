/**
 * Push notification registration — Expo's push service, backed by FCM.
 *
 * Registration is called once we know who's signed in (see the effect in
 * app/_layout.tsx), not at import time or app launch in general: the token
 * has to be attached to a specific account on the backend, and there's no
 * account to attach it to before sign-in resolves.
 *
 * Runs on a physical device only. `Device.isDevice` guards the two cases
 * that would otherwise throw or silently no-op: the Android emulator has no
 * real FCM registration to get a token from, and Expo Go dropped remote
 * push support after SDK 51 — this app is a standalone dev/production
 * build either way, but the guard costs nothing and avoids both traps.
 */

import { Platform } from 'react-native';
import Constants from 'expo-constants';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

import { notificationsApi } from '../api/endpoints';

// Foreground behaviour: a notification that arrives while the app is open
// still shows a banner and plays a sound, rather than the default
// (Android-historical) behaviour of silently doing nothing until the app is
// backgrounded. An order update is exactly the kind of thing worth
// interrupting for even if the app happens to be open.
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowAlert: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
    shouldShowBanner: true,
    shouldShowList: true,
  }),
});

let registering = false;

/**
 * Best-effort, always. A person who never grants permission, or isn't on a
 * real device, or hits a network hiccup mid-registration, simply gets no
 * push notifications — this must never throw into a sign-in flow or block
 * anything else the app is doing.
 */
export async function registerForPushNotifications(): Promise<void> {
  if (registering || !Device.isDevice) return;
  registering = true;
  try {
    if (Platform.OS === 'android') {
      await Notifications.setNotificationChannelAsync('default', {
        name: 'default',
        importance: Notifications.AndroidImportance.DEFAULT,
      });
    }

    const existing = await Notifications.getPermissionsAsync();
    let status = existing.status;
    if (status !== 'granted') {
      status = (await Notifications.requestPermissionsAsync()).status;
    }
    if (status !== 'granted') return;

    const projectId = Constants.expoConfig?.extra?.eas?.projectId as string | undefined;
    const { data: token } = await Notifications.getExpoPushTokenAsync(
      projectId ? { projectId } : undefined,
    );

    await notificationsApi.registerToken(token, Platform.OS === 'ios' ? 'ios' : 'android');
  } catch {
    // See the doc comment above — never surfaced to the user.
  } finally {
    registering = false;
  }
}
