/**
 * Push delivery on the device (UC23, UC15, UC17).
 *
 * Two halves that have to agree with the server:
 *
 * - **Registration.** The Expo push token is where the server sends a
 *   reminder. It is fetched and posted on every launch, not just the first,
 *   because Expo reissues it when the app is reinstalled or its storage is
 *   cleared — and a stale token is a reminder that goes nowhere, silently,
 *   with the device no longer around to ask.
 * - **Answering.** The Done and Snooze buttons come from a *category*
 *   registered here and named by the server on every message. The two names
 *   have to match exactly or the buttons simply do not appear, which is why
 *   both sides read them from one place.
 *
 * Both actions open the app (`opensAppToForeground`). The alternative reads
 * better on paper — answer without leaving the shade — but expo-notifications
 * says plainly what it costs: with `opensAppToForeground: false`, a response
 * given while the app is *killed* never reaches any listener at all. A Done
 * button that silently does nothing after a reboot is worse than one that
 * flashes the app open, so the reliable version is the one that ships. The
 * quiet version needs a native background handler, which is the same class of
 * work as UC24's alarm and waits for the same evidence.
 */
import { useEffect, useRef } from 'react';
import { Alert, Platform } from 'react-native';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';
import Constants from 'expo-constants';
import { router } from 'expo-router';

import { ApiError, markDone, registerDevice, snoozeItem } from './api';

/** Names the server also uses. `backend/config.py` holds the other copy. */
export const REMINDER_CATEGORY = 'shelf.reminder';
export const ANDROID_CHANNEL_ID = 'reminders';
/**
 * The weekly digest gets its own channel (UC31).
 *
 * Not a nicety: the reminder channel is HIGH importance because a due item is
 * a moment that has arrived, and a weekly summary that interrupts the same way
 * is how the owner ends up muting the channel that matters. Separate channels
 * mean the digest can be turned down without turning off reminders.
 */
export const DIGEST_CHANNEL_ID = 'digest';

export const ACTION_DONE = 'done';
export const ACTION_SNOOZE = 'snooze';

/**
 * A reminder is worth waking the screen for — it is about a moment that has
 * arrived, not a background update. Quiet hours were dropped (UC29), so
 * nothing here suppresses an overnight one either.
 */
Notifications.setNotificationHandler({
  handleNotification: async () => ({
    shouldShowBanner: true,
    shouldShowList: true,
    shouldPlaySound: true,
    shouldSetBadge: false,
  }),
});

/** Why registration did not happen, when it did not. */
export type RegistrationFailure =
  | 'emulator' // no Play Services, no token to get
  | 'denied' // the user said no
  | 'no-project' // the EAS project id is missing from the build
  | 'unavailable'; // Expo could not issue a token

export type RegistrationResult =
  | { ok: true; token: string }
  | { ok: false; reason: RegistrationFailure; detail?: string };

/** The EAS project id, which `getExpoPushTokenAsync` needs to mint a token. */
export function projectId(): string | undefined {
  const fromConfig = Constants.expoConfig?.extra?.eas?.projectId;
  if (typeof fromConfig === 'string' && fromConfig) return fromConfig;
  const fromEas = (Constants as { easConfig?: { projectId?: string } }).easConfig
    ?.projectId;
  return typeof fromEas === 'string' && fromEas ? fromEas : undefined;
}

/** Something to tell two phones apart by in the `push_tokens` table. */
function deviceName(): string | undefined {
  return (
    Device.deviceName ??
    [Device.manufacturer, Device.modelName].filter(Boolean).join(' ') ??
    undefined
  );
}

/**
 * Create the Android channel and the action buttons.
 *
 * Both are idempotent and both must exist before a push arrives: a message
 * naming a channel that was never created lands with default importance, and
 * one naming an unknown category arrives with no buttons.
 */
export async function configureChannels(): Promise<void> {
  await Notifications.setNotificationCategoryAsync(REMINDER_CATEGORY, [
    {
      identifier: ACTION_DONE,
      buttonTitle: 'Done',
      options: { opensAppToForeground: true },
    },
    {
      identifier: ACTION_SNOOZE,
      buttonTitle: 'Snooze',
      options: { opensAppToForeground: true },
    },
  ]);

  if (Platform.OS === 'android') {
    await Notifications.setNotificationChannelAsync(ANDROID_CHANNEL_ID, {
      name: 'Reminders',
      // The notification is the whole delivery mechanism (D3's "delivery must
      // be push, not pull"), so it gets to interrupt. Anything less than HIGH
      // is a heads-up notification that never appears as one.
      importance: Notifications.AndroidImportance.HIGH,
      sound: 'default',
      vibrationPattern: [0, 250, 250, 250],
      lockscreenVisibility: Notifications.AndroidNotificationVisibility.PUBLIC,
      enableVibrate: true,
    });

    await Notifications.setNotificationChannelAsync(DIGEST_CHANNEL_ID, {
      name: 'Weekly digest',
      // DEFAULT, not HIGH: it appears in the shade and waits. Once a week is
      // the whole point — it is an account of what already happened, and
      // nothing on it needs answering in the next minute.
      importance: Notifications.AndroidImportance.DEFAULT,
      sound: 'default',
      lockscreenVisibility: Notifications.AndroidNotificationVisibility.PRIVATE,
      enableVibrate: false,
    });
  }
}

/**
 * Get this device's push token and tell the server about it (UC23).
 *
 * Returns rather than throws: none of the ways this fails is worth an error
 * screen on a capture app, but every one of them is worth naming in a log,
 * because "no reminder arrived" is otherwise indistinguishable from "the
 * scheduler is broken".
 */
export async function registerForPush(): Promise<RegistrationResult> {
  if (!Device.isDevice) {
    // An emulator has no Play Services, so there is no token to be had.
    return { ok: false, reason: 'emulator' };
  }

  await configureChannels();

  const existing = await Notifications.getPermissionsAsync();
  const status = existing.granted
    ? existing.status
    : (await Notifications.requestPermissionsAsync()).status;

  if (status !== 'granted') return { ok: false, reason: 'denied' };

  const id = projectId();
  if (!id) return { ok: false, reason: 'no-project' };

  let token: string;
  try {
    token = (await Notifications.getExpoPushTokenAsync({ projectId: id })).data;
  } catch (e) {
    // Offline, or Expo could not reach FCM. The next launch tries again.
    console.error('[shelf/push] could not get a push token:', e);
    return {
      ok: false,
      reason: 'unavailable',
      detail: e instanceof Error ? e.message : String(e),
    };
  }

  await registerDevice({
    token,
    platform: Platform.OS === 'ios' ? 'ios' : 'android',
    device_name: deviceName(),
  });

  return { ok: true, token };
}

// ------------------------------------------------------------- responding

/**
 * Whether this notification is the weekly digest rather than a reminder.
 *
 * Told apart by the payload, not by the channel: a channel is an Android
 * delivery setting the user is free to change, and routing on something the
 * user can edit is how a tap ends up on the wrong screen.
 */
export function isDigest(response: Notifications.NotificationResponse): boolean {
  const data = response.notification.request.content.data as
    | Record<string, unknown>
    | undefined;
  return typeof data?.digest === 'string' && !!data.digest;
}

/** The item a notification is about, or null if the payload is not one of ours. */
export function itemIdFrom(response: Notifications.NotificationResponse): string | null {
  const data = response.notification.request.content.data as
    | Record<string, unknown>
    | undefined;
  const id = data?.itemId;
  return typeof id === 'string' && id ? id : null;
}

export type Outcome =
  | { kind: 'done' }
  | { kind: 'snoozed' }
  | { kind: 'opened' }
  | { kind: 'stale'; state: string }
  | { kind: 'failed'; message: string }
  | { kind: 'ignored' }; // not a notification we understand

/**
 * Do what the button that was pressed means (UC15, UC17).
 *
 * The body of the notification opens the item; the buttons act on it. Acting
 * can find that the item has moved on since the push went out — decayed to
 * the shelf, or finished on another surface — and that is reported rather
 * than treated as an error. The push was true when it was sent.
 */
export async function respondTo(
  response: Notifications.NotificationResponse,
): Promise<Outcome> {
  // The digest carries no item and no buttons — there is only one thing a tap
  // on it can mean, and any other action identifier on it is a dismissal.
  if (isDigest(response)) {
    if (response.actionIdentifier === Notifications.DEFAULT_ACTION_IDENTIFIER) {
      router.push('/digest');
      return { kind: 'opened' };
    }
    return { kind: 'ignored' };
  }

  const itemId = itemIdFrom(response);
  if (!itemId) return { kind: 'ignored' };

  const action = response.actionIdentifier;

  if (action === Notifications.DEFAULT_ACTION_IDENTIFIER) {
    router.push(`/item/${itemId}`);
    return { kind: 'opened' };
  }

  try {
    if (action === ACTION_DONE) {
      await markDone(itemId);
      return { kind: 'done' };
    }
    if (action === ACTION_SNOOZE) {
      const result = await snoozeItem(itemId);
      if (!result.changed) return { kind: 'stale', state: result.state };
      return { kind: 'snoozed' };
    }
  } catch (e) {
    console.error(`[shelf/push] ${action} failed for ${itemId}:`, e);
    return {
      kind: 'failed',
      message: e instanceof ApiError ? e.message : 'That did not go through.',
    };
  }

  // A dismissal, or an action identifier from an older build.
  return { kind: 'ignored' };
}

/** How an outcome is shown, if at all. */
export function announce(outcome: Outcome): void {
  if (outcome.kind === 'failed') {
    Alert.alert('Not saved', outcome.message);
    return;
  }
  if (outcome.kind === 'stale') {
    // Worth saying: the user pressed Snooze and the item did not move,
    // because it had already decayed. Silence here would look like a bug.
    Alert.alert(
      'That one has moved',
      `It is on the ${outcome.state === 'shelved' ? 'shelf' : outcome.state} now.`,
    );
    return;
  }
  if (outcome.kind === 'done' || outcome.kind === 'snoozed') {
    // No dialog. `Today` is where the answer shows: the row is gone.
    router.push('/today');
  }
}

/**
 * Register for pushes and answer the ones the user acts on.
 *
 * Mounted once, inside the session gate: a push token is a thing the server
 * stores against a user, so there has to be a user.
 *
 * @param enabled Whether there is a session to register against.
 */
export function usePushNotifications(enabled: boolean): void {
  const response = Notifications.useLastNotificationResponse();
  const handled = useRef(new Set<string>());

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;

    void registerForPush().then((result) => {
      if (cancelled || result.ok) return;
      // Not fatal, and not worth a dialog — but it is the difference between
      // "no reminders arrived" and "no reminders were sent".
      console.warn(`[shelf/push] not registered: ${result.reason}`, result.detail ?? '');
    });

    const subscription = Notifications.addPushTokenListener((token) => {
      // Expo rotated the token. Registering again is the only thing that
      // keeps reminders arriving.
      void registerDevice({
        token: token.data,
        platform: Platform.OS === 'ios' ? 'ios' : 'android',
        device_name: deviceName(),
      }).catch((e) => console.error('[shelf/push] re-registration failed:', e));
    });

    return () => {
      cancelled = true;
      subscription.remove();
    };
  }, [enabled]);

  useEffect(() => {
    // Without a session there is nobody to act as. The response is left
    // unhandled rather than dropped, so signing in picks it up.
    if (!enabled || !response) return;

    const key = `${response.notification.request.identifier}:${response.actionIdentifier}`;
    if (handled.current.has(key)) return;
    handled.current.add(key);

    void respondTo(response).then((outcome) => {
      announce(outcome);
      // Clear it so a cold start weeks later does not replay the same tap.
      // `markDone` would shrug that off; a second snooze would not.
      Notifications.clearLastNotificationResponse();
    });
  }, [enabled, response]);
}
