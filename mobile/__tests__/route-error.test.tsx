/**
 * A render failure has to be visible (D41).
 *
 * The tab bar bug was not an exception — nothing threw, the bar was simply six
 * pixels tall — so no boundary would have caught it. What it demonstrated is
 * the failure *mode* worth defending against: the app quietly degrading into
 * something that still looks like a working screen, so you go hunting for a
 * routing bug that is not there.
 *
 * These tests pin the two halves of that defence: the boundary is actually
 * wired into the layouts, and when it fires it says what broke rather than
 * rendering a shrug.
 */
import { render, screen } from '@testing-library/react-native';
import { renderRouter } from 'expo-router/testing-library';
import { Slot } from 'expo-router';
import { Text } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { RouteError } from '../lib/RouteError';

/** `RouteError` renders a `SafeAreaView`, which needs a provider above it. */
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <SafeAreaProvider
    initialMetrics={{
      insets: { top: 24, bottom: 48, left: 0, right: 0 },
      frame: { x: 0, y: 0, width: 412, height: 915 },
    }}
  >
    {children}
  </SafeAreaProvider>
);

jest.mock('expo-secure-store');
jest.mock('expo-file-system', () => ({ File: class {} }));

beforeEach(() => {
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  jest.spyOn(console, 'warn').mockImplementation(() => undefined);
});

afterEach(() => {
  jest.restoreAllMocks();
});

test('the failure names itself instead of showing a shrug', () => {
  const error = new Error('Cannot read properties of undefined');
  error.stack = 'Error: Cannot read properties of undefined\n    at Shelf (shelf.tsx:120)';

  render(<RouteError error={error} retry={async () => undefined} />, { wrapper });

  expect(screen.getByText(/This screen failed to render/)).toBeTruthy();
  // The message, not just the fact that something went wrong.
  expect(screen.getByText('Error: Cannot read properties of undefined')).toBeTruthy();
  expect(screen.getByText('Try again')).toBeTruthy();
});

test('a screen that throws surfaces the boundary rather than a bare screen', () => {
  // The wiring, not the component: expo-router picks the boundary up by the
  // name `ErrorBoundary` on the layout module, and a typo in that export is
  // silent — you get the default screen and never know yours was ignored.
  renderRouter(
    {
      _layout: {
        default: () => <Slot />,
        ErrorBoundary: RouteError,
      },
      index: () => {
        throw new Error('boom from a screen');
      },
    },
    { initialUrl: '/' },
  );

  expect(screen.getByText(/This screen failed to render/)).toBeTruthy();
  // Twice over: once as the message, once inside the stack.
  expect(screen.getAllByText(/boom from a screen/).length).toBeGreaterThan(0);
});

test('a screen that renders is left alone', () => {
  renderRouter(
    {
      _layout: {
        default: () => <Slot />,
        ErrorBoundary: RouteError,
      },
      index: () => <Text>all fine</Text>,
    },
    { initialUrl: '/' },
  );

  expect(screen.getByText('all fine')).toBeTruthy();
  expect(screen.queryByText(/This screen failed to render/)).toBeNull();
});
