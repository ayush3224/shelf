/**
 * The tab bar has to survive a bottom safe-area inset (D41).
 *
 * This is a regression test for a bug that was invisible in every other test
 * and total on the device. `app/(tabs)/_layout.tsx` set `tabBarStyle.height`
 * to a literal 60. React Navigation's `getTabBarHeight` returns a numeric
 * custom height verbatim — it only adds the inset on the path where it
 * computes the height itself — and then applies `paddingBottom: insets.bottom`
 * underneath regardless. On a phone with gesture navigation that left
 * `60 - 6 - 48 = 6dp` for the labels: a white strip a hairline tall on a
 * near-white background, which reads as no tab bar at all. Every tab in the
 * app was unreachable and nothing threw.
 *
 * A test with zero insets — which is every test that does not do what this one
 * does — renders the same code perfectly. So the inset is the whole point:
 * these assertions are about the arithmetic, not about the tabs existing.
 */
import { act } from 'react';
import { renderRouter, screen } from 'expo-router/testing-library';

/** A modern Android phone with gesture navigation. */
const INSETS = { top: 24, bottom: 48, left: 0, right: 0 };
const FRAME = { x: 0, y: 0, width: 412, height: 915 };

jest.mock('react-native-safe-area-context', () => {
  const actual = jest.requireActual('react-native-safe-area-context');
  const React = jest.requireActual('react');
  return {
    ...actual,
    useSafeAreaInsets: () => INSETS,
    // The app's root layout renders its own provider, so overriding the
    // provider is what puts a real inset in front of the navigator.
    SafeAreaProvider: ({ children }: { children: unknown }) =>
      React.createElement(
        actual.SafeAreaFrameContext.Provider,
        { value: FRAME },
        React.createElement(
          actual.SafeAreaInsetsContext.Provider,
          { value: INSETS },
          children,
        ),
      ),
  };
});

jest.mock('expo-secure-store');
jest.mock('expo-device');
jest.mock('expo-notifications');
jest.mock('expo-web-browser', () => ({
  openAuthSessionAsync: jest.fn(async () => ({ type: 'cancel' })),
  warmUpAsync: jest.fn(),
  coolDownAsync: jest.fn(),
}));
jest.mock('expo-auth-session', () => ({ makeRedirectUri: () => 'shelf://auth' }));
jest.mock('expo-file-system', () => ({ File: class {} }));
jest.mock('@react-native-community/datetimepicker', () => 'DateTimePicker');
jest.mock('expo-audio', () => ({
  RecordingPresets: { HIGH_QUALITY: {} },
  AudioModule: {},
  getRecordingPermissionsAsync: jest.fn(async () => ({ granted: true })),
  requestRecordingPermissionsAsync: jest.fn(async () => ({ granted: true })),
  setAudioModeAsync: jest.fn(async () => undefined),
  useAudioRecorder: () => ({
    prepareToRecordAsync: jest.fn(),
    record: jest.fn(),
    stop: jest.fn(),
    uri: null,
  }),
  useAudioPlayer: () => ({ play: jest.fn(), pause: jest.fn(), remove: jest.fn() }),
  createAudioPlayer: () => ({ play: jest.fn(), pause: jest.fn(), remove: jest.fn() }),
}));

// Signed in, so the guard opens onto the tabs. Everything else in `lib/auth`
// stays real — `+native-intent.tsx` imports `isAuthCallbackUrl` from it.
jest.mock('../lib/auth', () => {
  const actual = jest.requireActual('../lib/auth');
  // One object, built once. The real `AuthProvider` memoises its context value
  // on `[session, loading, signingIn, error]`, so `signOut` keeps its identity
  // across renders. A mock that rebuilds it every render is not the app: the
  // Shelf's `load` effect depends on it transitively, so it refetches forever
  // and takes the runner's heap with it.
  const value = {
    session: { access_token: 'test-token', user: { id: 'u' } },
    loading: false,
    signOut: jest.fn(),
  };
  return {
    ...actual,
    AuthProvider: ({ children }: { children: unknown }) => children,
    useAuth: () => value,
  };
});

beforeEach(() => {
  globalThis.fetch = jest.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({
      items: [],
      projects: [],
      as_of: new Date().toISOString(),
      next_cursor: null,
      has_more: false,
      states: [],
    }),
    text: async () => '{}',
  })) as never;
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  jest.spyOn(console, 'warn').mockImplementation(() => undefined);
});

afterEach(() => {
  jest.restoreAllMocks();
});

type Node = {
  type?: string;
  props?: Record<string, unknown>;
  children?: unknown[];
};

/** Every node in the rendered tree, flattened. */
function nodes(root: unknown): Node[] {
  const found: Node[] = [];
  const walk = (n: unknown): void => {
    if (!n || typeof n !== 'object') return;
    if (Array.isArray(n)) {
      n.forEach(walk);
      return;
    }
    const node = n as Node;
    found.push(node);
    (node.children ?? []).forEach(walk);
  };
  walk(root);
  return found;
}

/** Resolve a style prop, which may be an array, into one object. */
function flatten(style: unknown): Record<string, unknown> {
  if (!style) return {};
  if (Array.isArray(style)) {
    return style.reduce<Record<string, unknown>>(
      (acc, s) => ({ ...acc, ...flatten(s) }),
      {},
    );
  }
  return typeof style === 'object' ? (style as Record<string, unknown>) : {};
}

/**
 * The tab bar container: the node React Navigation gives both an explicit
 * height and a bottom padding. Found by shape rather than by testID, because
 * the view belongs to the navigator and we do not get to label it.
 */
function tabBarStyle(): Record<string, unknown> {
  const candidates = nodes(screen.toJSON())
    .map((n) => flatten(n.props?.style))
    .filter((s) => typeof s.height === 'number' && s.paddingBottom !== undefined);

  expect(candidates.length).toBeGreaterThan(0);
  return candidates[0];
}

async function mount(): Promise<void> {
  renderRouter('./app', { initialUrl: '/' });
  await act(async () => {
    await Promise.resolve();
  });
}

test('all three tabs render', async () => {
  await mount();

  // `getAllByText` rather than `getByText`: the capture screen's own heading is
  // the word "Shelf" too, so the tab label is not the only match.
  for (const label of ['Capture', 'Today', 'Shelf']) {
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  }
});

test('the inset is added to the height, not absorbed by it', async () => {
  await mount();
  const style = tabBarStyle();

  // Stated on the height alone, independent of any top padding: whatever the
  // bar is for, it has to be that tall *plus* the system gesture area, because
  // the gesture area is not space the bar gets to use. The old literal 60 left
  // 12 here, which is why this is an assertion and not a comment.
  expect((style.height as number) - INSETS.bottom).toBeGreaterThanOrEqual(20);
});

test('the labels have room to render after padding is taken out', async () => {
  await mount();
  const style = tabBarStyle();

  const height = style.height as number;
  const top = (style.paddingTop as number) ?? 0;
  const bottom = (style.paddingBottom as number) ?? 0;
  const forLabels = height - top - bottom;

  // A 13px label needs roughly 18dp of line box. The bug left 6.
  expect(forLabels).toBeGreaterThanOrEqual(20);
});
