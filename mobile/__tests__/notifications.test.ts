/**
 * Push registration and answering a notification (UC23, UC15, UC17).
 *
 * The two things worth pinning:
 *
 * - The category and channel names must be exactly what the server puts on
 *   every message. If they drift, the notification still arrives and the Done
 *   and Snooze buttons simply are not on it — a failure with no error anywhere.
 * - An action taken on a stale notification must not read as a failure. The
 *   item may have decayed since the push went out, and the app has to be able
 *   to say so.
 */
import { Platform } from 'react-native';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

import {
  ACTION_DONE,
  ACTION_SNOOZE,
  ANDROID_CHANNEL_ID,
  REMINDER_CATEGORY,
  itemIdFrom,
  registerForPush,
  respondTo,
} from '../lib/notifications';
import * as api from '../lib/api';

const ID = 'c1d2e3f4-0000-4000-8000-000000000001';

jest.mock('expo-notifications');
jest.mock('expo-device');
jest.mock('expo-secure-store');
jest.mock('expo-file-system', () => ({ File: class {} }));
jest.mock('expo-auth-session', () => ({
  makeRedirectUri: () => 'shelf://auth-callback',
}));
jest.mock('expo-web-browser', () => ({
  openAuthSessionAsync: jest.fn(),
  warmUpAsync: jest.fn(),
  coolDownAsync: jest.fn(),
}));
jest.mock('expo-constants', () => ({
  __esModule: true,
  default: { expoConfig: { extra: { eas: { projectId: 'test-project' } } } },
}));

const mockPush = jest.fn();
jest.mock('expo-router', () => ({
  router: { push: (...args: unknown[]) => mockPush(...args) },
}));

type Mocked = typeof Notifications & { __reset: () => void };
type MockedDevice = typeof Device & { __setIsDevice: (v: boolean) => void };

/** A notification response as the OS hands it over. */
function response(
  actionIdentifier: string,
  data: Record<string, unknown> = { itemId: ID },
): Notifications.NotificationResponse {
  return {
    actionIdentifier,
    notification: {
      request: { identifier: 'req-1', content: { data }, trigger: null },
    },
  } as unknown as Notifications.NotificationResponse;
}

beforeEach(() => {
  // jest-expo defaults to iOS. This app ships Android only, and the Android
  // path is the one with a notification channel in it — testing the other one
  // would prove nothing about what runs.
  Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true });
  (Notifications as Mocked).__reset();
  (Device as MockedDevice).__setIsDevice(true);
  mockPush.mockClear();
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());

// ------------------------------------------------------------ registration

describe('registering for push (UC23)', () => {
  it('posts the Expo token to the server with the platform', async () => {
    const registerDevice = jest
      .spyOn(api, 'registerDevice')
      .mockResolvedValue({ registered: true, devices: 1 });

    const result = await registerForPush();

    expect(result).toEqual({ ok: true, token: 'ExponentPushToken[test-token]' });
    expect(registerDevice).toHaveBeenCalledWith(
      expect.objectContaining({
        token: 'ExponentPushToken[test-token]',
        platform: 'android',
      }),
    );
  });

  it('registers the category the server names on every message', async () => {
    jest.spyOn(api, 'registerDevice').mockResolvedValue({ registered: true, devices: 1 });

    await registerForPush();

    const [identifier, actions] = (
      Notifications.setNotificationCategoryAsync as jest.Mock
    ).mock.calls[0];
    expect(identifier).toBe(REMINDER_CATEGORY);
    expect(actions.map((a: { identifier: string }) => a.identifier)).toEqual([
      ACTION_DONE,
      ACTION_SNOOZE,
    ]);
  });

  it('opens the app for both actions, so a response is never dropped', async () => {
    // With opensAppToForeground false, a response given while the app is
    // killed reaches no listener at all — a Done button that does nothing.
    jest.spyOn(api, 'registerDevice').mockResolvedValue({ registered: true, devices: 1 });

    await registerForPush();

    const [, actions] = (Notifications.setNotificationCategoryAsync as jest.Mock).mock
      .calls[0];
    for (const action of actions) {
      expect(action.options.opensAppToForeground).toBe(true);
    }
  });

  it('creates the Android channel the server names', async () => {
    jest.spyOn(api, 'registerDevice').mockResolvedValue({ registered: true, devices: 1 });

    await registerForPush();

    const [id, config] = (Notifications.setNotificationChannelAsync as jest.Mock).mock
      .calls[0];
    expect(id).toBe(ANDROID_CHANNEL_ID);
    // Anything below HIGH never appears as a heads-up notification, which is
    // the entire delivery mechanism.
    expect(config.importance).toBe(Notifications.AndroidImportance.HIGH);
  });

  it('does not ask an emulator for a token it cannot have', async () => {
    (Device as MockedDevice).__setIsDevice(false);
    const registerDevice = jest.spyOn(api, 'registerDevice');

    expect(await registerForPush()).toEqual({ ok: false, reason: 'emulator' });
    expect(registerDevice).not.toHaveBeenCalled();
  });

  it('stops at a refused permission rather than registering nothing', async () => {
    (Notifications.getPermissionsAsync as jest.Mock).mockResolvedValue({
      status: 'undetermined',
      granted: false,
    });
    (Notifications.requestPermissionsAsync as jest.Mock).mockResolvedValue({
      status: 'denied',
      granted: false,
    });
    const registerDevice = jest.spyOn(api, 'registerDevice');

    expect(await registerForPush()).toEqual({ ok: false, reason: 'denied' });
    expect(registerDevice).not.toHaveBeenCalled();
  });

  it('asks for permission only when it does not already have it', async () => {
    jest.spyOn(api, 'registerDevice').mockResolvedValue({ registered: true, devices: 1 });

    await registerForPush();

    expect(Notifications.requestPermissionsAsync).not.toHaveBeenCalled();
  });

  it('reports a token it could not get instead of throwing', async () => {
    // Offline at launch is ordinary. The next launch tries again; an error
    // screen on a capture app is not the answer.
    (Notifications.getExpoPushTokenAsync as jest.Mock).mockRejectedValue(
      new Error('network'),
    );

    const result = await registerForPush();

    expect(result).toMatchObject({ ok: false, reason: 'unavailable' });
  });
});

// -------------------------------------------------------------- responding

describe('reading a notification', () => {
  it('finds the item it is about', () => {
    expect(itemIdFrom(response(ACTION_DONE))).toBe(ID);
  });

  it('returns null for a payload that is not one of ours', () => {
    expect(itemIdFrom(response(ACTION_DONE, {}))).toBeNull();
    expect(itemIdFrom(response(ACTION_DONE, { itemId: 42 }))).toBeNull();
  });
});

describe('answering a notification (UC15, UC17)', () => {
  it('marks the item done', async () => {
    const markDone = jest
      .spyOn(api, 'markDone')
      .mockResolvedValue({ id: ID, state: 'done', changed: true });

    expect(await respondTo(response(ACTION_DONE))).toEqual({ kind: 'done' });
    expect(markDone).toHaveBeenCalledWith(ID);
  });

  it('snoozes without naming a duration, so the server owns that number', async () => {
    const snooze = jest.spyOn(api, 'snoozeItem').mockResolvedValue({
      id: ID,
      state: 'active',
      due_at: '2026-08-23T12:30:00Z',
      snooze_count: 1,
      changed: true,
    });

    expect(await respondTo(response(ACTION_SNOOZE))).toEqual({ kind: 'snoozed' });
    expect(snooze).toHaveBeenCalledWith(ID);
  });

  it('reports a stale notification rather than failing on it', async () => {
    // The push was true when it was sent; by the time the button is pressed
    // the item may have decayed to the shelf.
    jest.spyOn(api, 'snoozeItem').mockResolvedValue({
      id: ID,
      state: 'shelved',
      due_at: null,
      snooze_count: 3,
      changed: false,
    });

    expect(await respondTo(response(ACTION_SNOOZE))).toEqual({
      kind: 'stale',
      state: 'shelved',
    });
  });

  it('opens the item when the notification itself is tapped', async () => {
    const outcome = await respondTo(response(Notifications.DEFAULT_ACTION_IDENTIFIER));

    expect(outcome).toEqual({ kind: 'opened' });
    expect(mockPush).toHaveBeenCalledWith(`/item/${ID}`);
  });

  it('reports a failure instead of pretending the item moved', async () => {
    jest
      .spyOn(api, 'markDone')
      .mockRejectedValue(new api.ApiError(0, 'No connection.', 'transport'));

    expect(await respondTo(response(ACTION_DONE))).toEqual({
      kind: 'failed',
      message: 'No connection.',
    });
  });

  it('ignores an action from an older build', async () => {
    const markDone = jest.spyOn(api, 'markDone');

    expect(await respondTo(response('archive'))).toEqual({ kind: 'ignored' });
    expect(markDone).not.toHaveBeenCalled();
  });

  it('ignores a notification with no item on it', async () => {
    expect(await respondTo(response(ACTION_DONE, {}))).toEqual({ kind: 'ignored' });
  });
});
