/**
 * Unlinking, and the one question it asks (UC45, UC46, D58).
 *
 * An unlink is a correction to the filing: the item, its words and its
 * recording survive it, and linking back undoes it. So it does not confirm —
 * except where it is about to empty somebody who goes by other names, because
 * removing them discards those names and relinking does not bring them back.
 *
 * The rule lives on the server, which answers 409 rather than doing it. What
 * is pinned here is the client's half: that a 409 becomes a question and not
 * an error message, that saying no changes nothing, and that saying yes
 * repeats the request with the confirmation on it.
 */
import { Alert } from 'react-native';
import { render, screen, fireEvent, act } from '@testing-library/react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { unlinkPerson } from '../lib/unlinkPerson';
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

const PRIYA = { id: 'p1', name: 'Priya Sharma', aliases: ['Priya', 'P'] };
const PANSY = { id: 'p2', name: 'Pansy', aliases: [] };

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

/** Press one button of whatever `Alert.alert` was last handed. */
function answer(label: string): void {
  const mock = Alert.alert as unknown as jest.Mock;
  const buttons = mock.mock.calls.at(-1)?.[2] as
    | { text: string; onPress?: () => void }[]
    | undefined;
  buttons?.find((b) => b.text === label)?.onPress?.();
}

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  jest.spyOn(Alert, 'alert').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());

describe('the ordinary unlink', () => {
  it('does not ask, because linking back undoes it', async () => {
    const sent = stubApi([ok()]);

    await unlinkPerson(ITEM, PRIYA);

    expect(Alert.alert).not.toHaveBeenCalled();
    expect(sent).toHaveLength(1);
    expect(sent[0].method).toBe('DELETE');
    expect(sent[0].url).toContain(`/items/${ITEM}/people/p1`);
    // No confirmation on a request nobody was asked about.
    expect(sent[0].url).not.toContain('remove_person');
  });

  it('does not ask when emptying somebody who has no other names', async () => {
    // The commonest unlink there is — a name heard once that was never a
    // person. The server allows it outright, so no 409 ever arrives.
    const sent = stubApi([ok(true)]);

    const result = await unlinkPerson(ITEM, PANSY);

    expect(Alert.alert).not.toHaveBeenCalled();
    expect(result?.person_removed).toBe(true);
    expect(sent).toHaveLength(1);
  });
});

describe('emptying somebody who goes by other names', () => {
  it('turns the refusal into a question that names them', async () => {
    stubApi([{ status: 409 }, ok(true)]);

    const pending = unlinkPerson(ITEM, PRIYA);
    await act(async () => {
      await Promise.resolve();
    });

    const [title, body] = (Alert.alert as unknown as jest.Mock).mock.calls[0];
    expect(title).toContain('Priya Sharma');
    expect(body).toContain('“Priya”');
    expect(body).toContain('“P”');
    // The distinction the whole dialog exists to make.
    expect(body).toContain('Nothing you said is deleted');

    answer('Remove');
    await pending;
  });

  it('repeats the request with the confirmation when told to', async () => {
    const sent = stubApi([{ status: 409 }, ok(true)]);

    const pending = unlinkPerson(ITEM, PRIYA);
    await act(async () => {
      await Promise.resolve();
    });
    answer('Remove');
    const result = await pending;

    expect(sent).toHaveLength(2);
    expect(sent[1].url).toContain('remove_person=true');
    expect(result?.person_removed).toBe(true);
  });

  it('changes nothing when told no', async () => {
    const sent = stubApi([{ status: 409 }]);

    const pending = unlinkPerson(ITEM, PRIYA);
    await act(async () => {
      await Promise.resolve();
    });
    answer('Cancel');

    // Null rather than a throw: the caller has nothing to report and nothing
    // to repair. The first request already left the link where it was.
    expect(await pending).toBeNull();
    expect(sent).toHaveLength(1);
  });

  it('lets every other failure through as a failure', async () => {
    stubApi([{ status: 500 }]);

    await expect(unlinkPerson(ITEM, PRIYA)).rejects.toThrow();
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
