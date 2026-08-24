/**
 * The Shelf's filter chips must size to their labels (D42).
 *
 * Second instance of the family the tab bar belongs to (D41): a container whose
 * height does not come from the text inside it. The mechanism differs — there
 * is no literal here — but the symptom was the same, both chip rows cut off
 * mid-text on the device while every test passed.
 *
 * A `ScrollView` defaults to `flexGrow: 1, flexShrink: 1` (RN's
 * `styles.baseHorizontal`). The Shelf stacks three of them in a column next to
 * a `SectionList`, which is another one, so four siblings are all willing to
 * give up height; when the page overflows they shrink together and the chip
 * rows — holding the least — lose their labels first.
 *
 * jest does no layout, so none of this can assert a rendered height. What it
 * can pin is the property that decides one, which is exactly what was missing.
 */
import { act } from 'react';
import { renderRouter, screen } from 'expo-router/testing-library';

// Mounting the whole router and settling three screens is not a 5s job here.
jest.setTimeout(30000);

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

/** Resolve a style prop, which may be an array, into one object. */
function flatten(style: unknown): Record<string, unknown> {
  if (!style) return {};
  if (Array.isArray(style)) {
    return style.reduce<Record<string, unknown>>((a, x) => ({ ...a, ...flatten(x) }), {});
  }
  return typeof style === 'object' ? (style as Record<string, unknown>) : {};
}

/** The style each chip scroller actually resolved. */
function chipRowStyles(): Record<string, unknown>[] {
  return screen.getAllByTestId('chip-row').map((n) => flatten(n.props.style));
}

async function mountShelf(): Promise<void> {
  renderRouter('./app', { initialUrl: '/shelf' });
  await act(async () => {
    await Promise.resolve();
  });
}

test('the chip rows are on screen', async () => {
  await mountShelf();

  for (const label of ['Shelved', 'Done', 'Dropped', 'Active', 'Any time', '7 days']) {
    expect(screen.getAllByText(label).length).toBeGreaterThan(0);
  }
});

test('a chip row never gives up height to its siblings', async () => {
  await mountShelf();
  const rows = chipRowStyles();

  // Two by default; the project row stays hidden until a project exists.
  expect(rows.length).toBeGreaterThanOrEqual(2);
  for (const style of rows) {
    // RN's default is 1 for both, which is the bug. Shrink is what clipped the
    // labels; grow is what would stretch one row down the page.
    expect(style.flexShrink).toBe(0);
    expect(style.flexGrow).toBe(0);
  }
});

test('no chip row sets a height of its own', async () => {
  await mountShelf();

  for (const style of chipRowStyles()) {
    expect(style.height).toBeUndefined();
    expect(style.maxHeight).toBeUndefined();
  }
});

test('the rows are still horizontal scrollers', async () => {
  await mountShelf();

  // Sizing to content must not have cost the scroll: the chips are wider than
  // the screen and the later ones are only reachable by dragging.
  for (const row of screen.getAllByTestId('chip-row')) {
    expect(row.props.horizontal).toBe(true);
  }
});
