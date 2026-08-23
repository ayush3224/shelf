/**
 * A stand-in for the notification module.
 *
 * Only the surface `lib/notifications` actually uses, and no more: a mock that
 * invents methods lets a test pass against an API the real module does not
 * have, which is exactly the failure this project has already paid for twice
 * (D26, D28).
 */
export const DEFAULT_ACTION_IDENTIFIER = 'expo.modules.notifications.actions.DEFAULT';

export enum AndroidImportance {
  MIN = 3,
  LOW = 4,
  DEFAULT = 5,
  HIGH = 6,
  MAX = 7,
}

export enum AndroidNotificationVisibility {
  PRIVATE = 0,
  PUBLIC = 1,
  SECRET = -1,
}

export const setNotificationHandler = jest.fn();
export const setNotificationCategoryAsync = jest.fn(async () => undefined);
export const setNotificationChannelAsync = jest.fn(async () => undefined);
export const clearLastNotificationResponse = jest.fn();

export const getPermissionsAsync = jest.fn(async () => ({
  status: 'granted',
  granted: true,
}));
export const requestPermissionsAsync = jest.fn(async () => ({
  status: 'granted',
  granted: true,
}));

export const getExpoPushTokenAsync = jest.fn(async () => ({
  data: 'ExponentPushToken[test-token]',
  type: 'expo' as const,
}));

export const addPushTokenListener = jest.fn(() => ({ remove: jest.fn() }));
export const useLastNotificationResponse = jest.fn(() => null);

/** Reset every mock to its default answer. */
export function __reset(): void {
  setNotificationHandler.mockClear();
  setNotificationCategoryAsync.mockClear();
  setNotificationChannelAsync.mockClear();
  clearLastNotificationResponse.mockClear();
  addPushTokenListener.mockClear();
  getPermissionsAsync.mockReset();
  getPermissionsAsync.mockResolvedValue({ status: 'granted', granted: true });
  requestPermissionsAsync.mockReset();
  requestPermissionsAsync.mockResolvedValue({ status: 'granted', granted: true });
  getExpoPushTokenAsync.mockReset();
  getExpoPushTokenAsync.mockResolvedValue({
    data: 'ExponentPushToken[test-token]',
    type: 'expo',
  });
}
