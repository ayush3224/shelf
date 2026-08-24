/**
 * The weekly digest on the device (UC31).
 *
 * Three things, all of which fail silently if they are wrong:
 *
 * - **Routing.** The digest push carries no `itemId`, so the reminder path
 *   would classify it as "not one of ours" and a tap would do nothing at all.
 * - **Its own channel.** The reminder channel is HIGH importance; if the
 *   digest shared it, a weekly summary would interrupt like a due item and the
 *   owner would mute the channel that matters.
 * - **The week's label.** `period_end` is exclusive, so printing it directly
 *   claims the digest covers a day it does not.
 */
import { Platform } from 'react-native';
import * as Device from 'expo-device';
import * as Notifications from 'expo-notifications';

import {
  DIGEST_CHANNEL_ID,
  isDigest,
  itemIdFrom,
  registerForPush,
  respondTo,
} from '../lib/notifications';
import { dropsInLabel, weekLabel } from '../lib/time';
import * as api from '../lib/api';

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
  data: Record<string, unknown>,
): Notifications.NotificationResponse {
  return {
    actionIdentifier,
    notification: {
      request: { identifier: 'req-1', content: { data }, trigger: null },
    },
  } as unknown as Notifications.NotificationResponse;
}

/** What the server actually sends for a digest: a week, and nothing else. */
const DIGEST_DATA = { digest: '2026-08-16' };

beforeEach(() => {
  Object.defineProperty(Platform, 'OS', { value: 'android', configurable: true });
  (Notifications as Mocked).__reset();
  (Device as MockedDevice).__setIsDevice(true);
  mockPush.mockClear();
});

afterEach(() => jest.restoreAllMocks());

// ------------------------------------------------------------------ channel

describe('the digest channel (UC31)', () => {
  it('is created alongside the reminder channel', async () => {
    jest.spyOn(api, 'registerDevice').mockResolvedValue({ registered: true, devices: 1 });

    await registerForPush();

    const calls = (Notifications.setNotificationChannelAsync as jest.Mock).mock.calls;
    const digest = calls.find(([id]) => id === DIGEST_CHANNEL_ID);
    expect(digest).toBeDefined();
  });

  it('is quieter than the reminder channel, so muting one does not mute both', async () => {
    jest.spyOn(api, 'registerDevice').mockResolvedValue({ registered: true, devices: 1 });

    await registerForPush();

    const calls = (Notifications.setNotificationChannelAsync as jest.Mock).mock.calls;
    const [, config] = calls.find(([id]) => id === DIGEST_CHANNEL_ID)!;
    expect(config.importance).toBe(Notifications.AndroidImportance.DEFAULT);
    expect(config.enableVibrate).toBe(false);
  });
});

// ------------------------------------------------------------------ routing

describe('tapping the digest (UC31)', () => {
  it('is recognised by its payload, not by its channel', () => {
    // A channel is an Android setting the user can change. Routing on it is
    // how a tap ends up on the wrong screen a month after somebody fiddles
    // with their notification settings.
    expect(isDigest(response('default', DIGEST_DATA))).toBe(true);
    expect(isDigest(response('default', { itemId: 'abc' }))).toBe(false);
  });

  it('opens the digest rather than doing nothing', async () => {
    // The failure this pins: with no `itemId` the reminder path reads the
    // digest as "not one of ours" and the tap is silently discarded.
    expect(itemIdFrom(response('default', DIGEST_DATA))).toBeNull();

    const outcome = await respondTo(
      response(Notifications.DEFAULT_ACTION_IDENTIFIER, DIGEST_DATA),
    );

    expect(outcome).toEqual({ kind: 'opened' });
    expect(mockPush).toHaveBeenCalledWith('/digest');
  });

  it('ignores a dismissal without opening anything', async () => {
    const outcome = await respondTo(response('dismiss', DIGEST_DATA));

    expect(outcome).toEqual({ kind: 'ignored' });
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('never tries to mark a week done', async () => {
    // The digest carries no category, so it has no buttons — but an old build
    // could still deliver one, and acting on it would need an item there is
    // no id for.
    const markDone = jest.spyOn(api, 'markDone');

    expect(await respondTo(response('done', DIGEST_DATA))).toEqual({ kind: 'ignored' });
    expect(markDone).not.toHaveBeenCalled();
  });
});

// ------------------------------------------------------------------- labels

describe('what the week is called', () => {
  it('names the last day it covers, not the exclusive bound', () => {
    // 16 to 23 August, half-open: the digest is about the week up to Saturday
    // night, and printing "23 Aug" would claim a day it does not cover.
    expect(weekLabel('2026-08-16T09:00:00Z', '2026-08-23T09:00:00Z')).toContain('16');
    expect(weekLabel('2026-08-16T09:00:00Z', '2026-08-23T09:00:00Z')).toContain('22');
  });

  it('says nothing at all for an unparseable week', () => {
    expect(weekLabel('not-a-date', '2026-08-23T09:00:00Z')).toBe('');
  });
});

describe('how long something has left', () => {
  const now = new Date('2026-08-24T12:00:00Z');

  it('rounds up, so a warning is never later than the sweep', () => {
    // 30 hours left is "tomorrow", not "in 1 day": the digest is the only
    // warning there is, and it should sound early rather than late.
    expect(dropsInLabel('2026-08-25T18:00:00Z', now)).toBe('Drops tomorrow');
  });

  it('counts whole days out', () => {
    expect(dropsInLabel('2026-08-29T12:00:00Z', now)).toBe('Drops in 5 days');
  });

  it('says today for anything already past', () => {
    expect(dropsInLabel('2026-08-24T09:00:00Z', now)).toBe('Drops today');
  });
});
