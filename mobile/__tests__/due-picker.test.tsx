/**
 * Changing when something is due asks for the day *and* the time (D61).
 *
 * It used to ask only for the date and keep whatever time of day the item
 * already had, which is wrong nearly every time: moving a thing to Thursday
 * means moving it to a different part of Thursday, and the item quietly kept
 * the 3pm it inherited from the sentence it was captured in.
 *
 * So the date question hands over to the time question and one edit is sent
 * when both are answered. What is pinned here is that pairing — that nothing
 * is written between the two legs, that the time leg starts from the day just
 * chosen, and that backing out of either leg writes nothing at all.
 */
import { render, screen, fireEvent, act } from '@testing-library/react-native';

import { supabase } from '../lib/supabase';

const ITEM = 'b3f0c1a2-0000-4000-8000-000000000001';
/** The item comes in due on a Monday afternoon. */
const DUE = new Date(2026, 7, 31, 15, 0, 0, 0);

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
  useLocalSearchParams: () => ({ id: 'b3f0c1a2-0000-4000-8000-000000000001' }),
}));
jest.mock('../lib/auth', () => {
  const value = {
    session: { access_token: 'test-token' },
    loading: false,
    signOut: jest.fn(),
  };
  return { useAuth: () => value };
});

/**
 * A picker that can be answered or dismissed from a test.
 *
 * The real one is a native dialog, so the two things worth driving — an answer
 * and a back-press — are exposed as buttons, and the `value` it opened at is
 * rendered so the second leg can be shown to start from the first leg's day.
 */
jest.mock('@react-native-community/datetimepicker', () => {
  const React = require('react');
  const { Pressable, Text } = require('react-native');
  const ANSWER: Record<string, Date> = {
    // A Thursday, and a time of day nothing on the item could have supplied.
    date: new Date(2026, 8, 3, 0, 0, 0, 0),
    time: new Date(2026, 0, 1, 9, 15, 0, 0),
  };
  return function MockPicker({
    value,
    mode,
    onChange,
  }: {
    value: Date;
    mode: 'date' | 'time';
    onChange: (event: unknown, picked?: Date) => void;
  }) {
    return React.createElement(React.Fragment, null, [
      React.createElement(
        Text,
        { key: 'v', testID: `picker-${mode}-value` },
        value.toISOString(),
      ),
      React.createElement(
        Pressable,
        {
          key: 'a',
          accessibilityLabel: `answer the ${mode}`,
          onPress: () => onChange({ type: 'set' }, ANSWER[mode]),
        },
        React.createElement(Text, null, 'answer'),
      ),
      React.createElement(
        Pressable,
        {
          key: 'd',
          accessibilityLabel: `dismiss the ${mode}`,
          onPress: () => onChange({ type: 'dismissed' }, undefined),
        },
        React.createElement(Text, null, 'dismiss'),
      ),
    ]);
  };
});

type Sent = { url: string; method?: string; body?: string };

function detail(over: Record<string, unknown> = {}) {
  return {
    id: ITEM,
    text: 'Call the insurance people',
    raw_text: 'uh call the insurance people monday at three',
    parsed_text: 'Call the insurance people',
    kind: 'task',
    state: 'active',
    due_at: DUE.toISOString(),
    critical: false,
    parse_status: 'ok',
    source: 'voice',
    has_audio: false,
    transcript_source: 'cloud',
    transcript_confidence: 0.9,
    created_at: '2026-08-24T09:00:00Z',
    updated_at: '2026-08-24T09:00:00Z',
    people: [],
    on_calendar: false,
    calendar_sync_state: null,
    calendar_stalled: false,
    ...over,
  };
}

/** Every request answers with the item; the PATCH body is what is inspected. */
function stubApi(row: Record<string, unknown>): Sent[] {
  const sent: Sent[] = [];
  globalThis.fetch = jest.fn(async (url: unknown, init: unknown) => {
    const request = init as RequestInit;
    sent.push({
      url: String(url),
      method: request?.method,
      body: request?.body as string | undefined,
    });
    return { ok: true, status: 200, json: async () => row, text: async () => '' };
  }) as unknown as typeof fetch;
  return sent;
}

async function open(over: Record<string, unknown> = {}) {
  const sent = stubApi(detail(over));
  const ItemScreen = require('../app/item/[id]').default;
  render(<ItemScreen />);
  await act(async () => {
    await Promise.resolve();
  });
  return sent;
}

/** Press something and let the promise it started settle. */
async function press(label: string): Promise<void> {
  await act(async () => {
    fireEvent.press(screen.getByLabelText(label));
  });
}

const patches = (sent: Sent[]) => sent.filter((s) => s.method === 'PATCH');

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());

describe('changing the date', () => {
  it('asks for the time before writing anything', async () => {
    const sent = await open();

    await press('Change the due date and time');
    await press('answer the date');

    // The whole point: a date on its own is not an edit yet.
    expect(patches(sent)).toHaveLength(0);
    expect(screen.getByLabelText('answer the time')).toBeTruthy();
  });

  it('opens the time leg on the day just chosen', async () => {
    await open();

    await press('Change the due date and time');
    await press('answer the date');

    // Not the day the item came in with — otherwise the second question is
    // being asked about the wrong thing.
    const opened = new Date(screen.getByTestId('picker-time-value').props.children);
    expect(opened.getFullYear()).toBe(2026);
    expect(opened.getMonth()).toBe(8);
    expect(opened.getDate()).toBe(3);
  });

  it('sends one edit carrying both answers', async () => {
    const sent = await open();

    await press('Change the due date and time');
    await press('answer the date');
    await press('answer the time');

    const written = patches(sent);
    expect(written).toHaveLength(1);
    const due = new Date(JSON.parse(written[0].body ?? '{}').due_at);
    expect([due.getFullYear(), due.getMonth(), due.getDate()]).toEqual([2026, 8, 3]);
    // 9:15, not the 3pm the item inherited. This is the bug D61 is about.
    expect([due.getHours(), due.getMinutes()]).toEqual([9, 15]);
  });

  it('writes nothing when the time leg is backed out of', async () => {
    const sent = await open();

    await press('Change the due date and time');
    await press('answer the date');
    await press('dismiss the time');

    // A cancel is a cancel. Saving the date alone is exactly the half-answer
    // this pairing exists to stop.
    expect(patches(sent)).toHaveLength(0);
    expect(screen.queryByLabelText('answer the time')).toBeNull();
  });

  it('writes nothing when the date leg is backed out of', async () => {
    const sent = await open();

    await press('Change the due date and time');
    await press('dismiss the date');

    expect(patches(sent)).toHaveLength(0);
    expect(screen.queryByLabelText('answer the time')).toBeNull();
  });
});

describe('changing only the time', () => {
  it('is still one question, and keeps the day', async () => {
    const sent = await open();

    await press('Change the due time');
    await press('answer the time');

    const written = patches(sent);
    expect(written).toHaveLength(1);
    const due = new Date(JSON.parse(written[0].body ?? '{}').due_at);
    expect([due.getFullYear(), due.getMonth(), due.getDate()]).toEqual([
      DUE.getFullYear(),
      DUE.getMonth(),
      DUE.getDate(),
    ]);
    expect([due.getHours(), due.getMinutes()]).toEqual([9, 15]);
  });
});

describe('an item with no time yet', () => {
  it('is given both, rather than a date and whatever o clock it is now', async () => {
    const sent = await open({ due_at: null, state: 'shelved' });

    await press('Change the due date and time');
    await press('answer the date');
    await press('answer the time');

    const written = patches(sent);
    expect(written).toHaveLength(1);
    const due = new Date(JSON.parse(written[0].body ?? '{}').due_at);
    expect([due.getHours(), due.getMinutes()]).toEqual([9, 15]);
  });
});
