/**
 * Unlinking, which no longer asks anything (UC45, UC46, D60).
 *
 * An unlink is a correction to the filing: the item, its words and its
 * recording survive it, and linking back undoes it. So it does not confirm.
 *
 * It did have one exception. Emptying somebody removes them and the names they
 * went by, and relinking brings back the person but not the names — a real
 * loss, and D58 put a dialog in front of it. D60 took the dialog away: the
 * question arrived on ordinary unlinks often enough that answering it cost more
 * than the names are worth. What is pinned here is that nothing asks any more,
 * in either direction, and that one press means one request.
 */
import { Alert } from 'react-native';
import { render, screen, fireEvent, act } from '@testing-library/react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { removeItemPerson } from '../lib/api';
import { supabase } from '../lib/supabase';

jest.mock('expo-secure-store');
jest.mock('expo-file-system', () => ({ File: class {} }));
jest.mock('expo-web-browser', () => ({
  openAuthSessionAsync: jest.fn(async () => ({ type: 'cancel' })),
  warmUpAsync: jest.fn(),
  coolDownAsync: jest.fn(),
}));
jest.mock('expo-auth-session', () => ({ makeRedirectUri: () => 'shelf://auth' }));
jest.mock('expo-audio', () => ({
  useAudioPlayer: () => ({ play: jest.fn(), pause: jest.fn(), remove: jest.fn() }),
  useAudioPlayerStatus: () => ({ playing: false, didJustFinish: false }),
  createAudioPlayer: () => ({ play: jest.fn(), pause: jest.fn(), remove: jest.fn() }),
}));
jest.mock('expo-router', () => ({
  Stack: { Screen: () => null },
  router: { push: jest.fn(), back: jest.fn(), replace: jest.fn() },
  useLocalSearchParams: () => ({ id: 'p1' }),
}));

// Signed in. One object built once, so `signOut` keeps its identity across
// renders — the screen's `load` effect depends on it, and a mock that rebuilds
// every render refetches forever.
jest.mock('../lib/auth', () => {
  const value = { session: { access_token: 'test-token' }, loading: false, signOut: jest.fn() };
  return { useAuth: () => value };
});

const ITEM = 'b3f0c1a2-0000-4000-8000-000000000001';

type Sent = { url: string; method?: string };

/** Answers each call in turn, so a retry can be given a different reply. */
function stubApi(replies: { status: number; body?: unknown }[]): Sent[] {
  const sent: Sent[] = [];
  let call = 0;
  globalThis.fetch = jest.fn(async (url: unknown, init: unknown) => {
    const reply = replies[Math.min(call++, replies.length - 1)];
    sent.push({ url: String(url), method: (init as RequestInit)?.method });
    return {
      ok: reply.status < 400,
      status: reply.status,
      json: async () => reply.body ?? { detail: 'nope' },
      text: async () => JSON.stringify(reply.body ?? {}),
    };
  }) as unknown as typeof fetch;
  return sent;
}

const ok = (personRemoved = false) => ({
  status: 200,
  body: { id: ITEM, people: [], changed: true, person_removed: personRemoved },
});

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  jest.spyOn(Alert, 'alert').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());

describe('the unlink', () => {
  it('is one request, with nothing to confirm on it', async () => {
    const sent = stubApi([ok()]);

    await removeItemPerson(ITEM, 'p1');

    expect(Alert.alert).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe('DELETE');
    expect(sent[0].url).toContain(`/items/${ITEM}/people/p1`);
    // The confirmation D58 added and D60 removed. Nothing sends it any more,
    // and the server no longer looks for it.
    expect(sent[0].url).not.toContain('remove_person');
  });

  it('does not ask before emptying somebody who goes by other names', async () => {
    // The case D58 existed for: Priya Sharma's last note, with "Priya" and "P"
    // recorded against her. It now goes the same way a cat named Pansy does.
    const sent = stubApi([ok(true)]);

    const result = await removeItemPerson(ITEM, 'p1');

    expect(Alert.alert).not.toHaveBeenCalled();
    expect(result.person_removed).toBe(true);
    expect(sent).toHaveLength(1);
  });

  it('lets a failure through as a failure', async () => {
    stubApi([{ status: 500 }]);

    await expect(removeItemPerson(ITEM, 'p1')).rejects.toThrow();
    expect(Alert.alert).not.toHaveBeenCalled();
  });
});

describe('the control on the person page', () => {
  const page = {
    person: {
      id: 'p1',
      name: 'Priya Sharma',
      type: 'person',
      aliases: ['Priya'],
      mentions: 2,
      last_mentioned: null,
    },
    items: [
      {
        id: ITEM,
        text: 'Call Priya about the invoice',
        raw_text: 'Call Priya about the invoice',
        kind: 'task',
        state: 'active',
        due_at: null,
        critical: false,
        parse_status: 'ok',
        has_audio: false,
        created_at: '2026-08-24T09:00:00Z',
      },
    ],
    next_cursor: null,
    has_more: false,
  };

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

  async function open(replies: { status: number; body?: unknown }[]) {
    const sent = stubApi([{ status: 200, body: page }, ...replies]);
    // Required late: the screen pulls in expo-router, which is mocked above.
    const PersonScreen = require('../app/person/[id]').default;
    render(<PersonScreen />, { wrapper });
    await act(async () => {
      await Promise.resolve();
    });
    return sent;
  }

  it('is on the row, in the same words as the chip on item detail', async () => {
    await open([]);

    // The repair is on the screen where the mistake is visible, not two
    // screens away — that is the whole change.
    expect(screen.getByLabelText('Not about Priya Sharma')).toBeTruthy();
  });

  it('unlinks that row without asking', async () => {
    const sent = await open([ok()]);

    await act(async () => {
      fireEvent.press(screen.getByLabelText('Not about Priya Sharma'));
    });

    expect(Alert.alert).not.toHaveBeenCalled();
    expect(sent[1].method).toBe('DELETE');
    expect(sent[1].url).toContain(`/items/${ITEM}/people/p1`);
    // Off the page, and the item itself untouched.
    expect(screen.queryByText('Call Priya about the invoice')).toBeNull();
  });

  it('is gone while notes are being selected to move', async () => {
    await open([]);

    await act(async () => {
      fireEvent.press(screen.getByLabelText('Select notes to move to someone else'));
    });

    // A row in selection mode is a checkbox; an unlink target sharing it is
    // how you take something off a page you meant to move it from.
    expect(screen.queryByLabelText('Not about Priya Sharma')).toBeNull();
  });
});
