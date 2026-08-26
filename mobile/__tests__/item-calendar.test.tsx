/**
 * Putting an item on the calendar, and taking it off (UC43, D59).
 *
 * Everything with a time used to sync on its own. That is the wrong default
 * here: most captures carry a time so a push knows when to fire, so the
 * calendar filled with reminders and the four things that were genuinely
 * appointments stopped being findable among them.
 *
 * What is pinned is the shape of the decision. A timed item is *offered* the
 * calendar and is not on it; the press is what puts it there; the same control
 * takes it back off; and an item with no time is not asked at all. Nothing here
 * waits on Google — the screen reports `pending` and the tick has a minute.
 */
import { render, screen, fireEvent, act } from '@testing-library/react-native';

import { supabase } from '../lib/supabase';

const ITEM = 'b3f0c1a2-0000-4000-8000-000000000001';

jest.mock('expo-secure-store');
jest.mock('expo-file-system', () => ({ File: class {} }));
jest.mock('expo-web-browser', () => ({
  openAuthSessionAsync: jest.fn(async () => ({ type: 'cancel' })),
  warmUpAsync: jest.fn(),
  coolDownAsync: jest.fn(),
}));
jest.mock('expo-auth-session', () => ({ makeRedirectUri: () => 'shelf://auth' }));
jest.mock('@react-native-community/datetimepicker', () => 'DateTimePicker');
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

type Sent = { url: string; method?: string };

function detail(over: Record<string, unknown> = {}) {
  return {
    id: ITEM,
    text: 'Dentist',
    raw_text: 'dentist thursday at nine fifteen',
    parsed_text: 'Dentist',
    kind: 'task',
    state: 'active',
    due_at: '2026-09-03T09:15:00+05:30',
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

/**
 * The item, then one scripted reply per request after it.
 *
 * The screen folds the reply into what it is showing rather than reloading, so
 * a stub that answered every call with the same row would hide the bug where
 * the button does not flip.
 */
function stubApi(row: Record<string, unknown>, replies: unknown[]): Sent[] {
  const sent: Sent[] = [];
  let call = 0;
  globalThis.fetch = jest.fn(async (url: unknown, init: unknown) => {
    const index = call++;
    sent.push({ url: String(url), method: (init as RequestInit)?.method });
    const body = index === 0 ? row : replies[index - 1];
    return { ok: true, status: 200, json: async () => body, text: async () => '' };
  }) as unknown as typeof fetch;
  return sent;
}

async function open(over: Record<string, unknown> = {}, replies: unknown[] = []) {
  const sent = stubApi(detail(over), replies);
  const ItemScreen = require('../app/item/[id]').default;
  render(<ItemScreen />);
  await act(async () => {
    await Promise.resolve();
  });
  return sent;
}

async function press(label: string): Promise<void> {
  await act(async () => {
    fireEvent.press(screen.getByLabelText(label));
  });
}

const added = (over: Record<string, unknown> = {}) => ({
  id: ITEM,
  on_calendar: true,
  changed: true,
  sync_state: 'pending',
  queued: false,
  ...over,
});

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
});

afterEach(() => jest.restoreAllMocks());

describe('a timed item', () => {
  it('is offered the calendar rather than put on it', async () => {
    const sent = await open();

    expect(screen.getByLabelText('Add this to your calendar')).toBeTruthy();
    // The change in one assertion: having a time is no longer having an event.
    expect(sent.filter((s) => s.url.includes('/calendar'))).toHaveLength(0);
  });

  it('goes on the calendar when the button is pressed', async () => {
    const sent = await open({}, [added()]);

    await press('Add this to your calendar');

    const call = sent.find((s) => s.url.includes('/calendar'));
    expect(call?.method).toBe('POST');
    expect(call?.url).toContain(`/items/${ITEM}/calendar`);
    // Flipped without a reload, and honest about not being there yet.
    expect(screen.getByLabelText('Remove this from your calendar')).toBeTruthy();
    expect(screen.getByText(/^Added/)).toBeTruthy();
  });

  it('comes back off it when the button is pressed again', async () => {
    const sent = await open(
      { on_calendar: true, calendar_sync_state: 'synced' },
      [{ id: ITEM, on_calendar: false, changed: true, sync_state: null, queued: true }],
    );

    await press('Remove this from your calendar');

    const call = sent.find((s) => s.url.includes('/calendar'));
    expect(call?.method).toBe('DELETE');
    expect(screen.getByLabelText('Add this to your calendar')).toBeTruthy();
  });

  it('says the item itself is untouched when it never reached Google', async () => {
    await open(
      { on_calendar: true, calendar_sync_state: 'pending' },
      [{ id: ITEM, on_calendar: false, changed: true, sync_state: null, queued: false }],
    );

    await press('Remove this from your calendar');

    // Taking it off the calendar is not cancelling the reminder, and the one
    // case where nothing has to come down is the one worth saying so on.
    expect(screen.getByText(/still reminds you/)).toBeTruthy();
  });
});

describe('a sync that gave up', () => {
  it('offers a retry, which the Remove button is not', async () => {
    const sent = await open(
      { on_calendar: true, calendar_sync_state: 'error', calendar_stalled: true },
      [added({ changed: false })],
    );

    expect(screen.getByText(/could not be reached/)).toBeTruthy();
    await press('Try adding it to your calendar again');

    const call = sent.find((s) => s.url.includes('/calendar'));
    expect(call?.method).toBe('POST');
    // Nothing else in the app resets a spent attempt count, so without this
    // the item is listed-but-absent until it happens to be edited.
    expect(screen.queryByLabelText('Try adding it to your calendar again')).toBeNull();
  });
});

describe('an item with no time', () => {
  it('is not offered the calendar at all', async () => {
    await open({ due_at: null, state: 'shelved' });

    // There is nothing to put in a day. The server would refuse it too.
    expect(screen.queryByLabelText('Add this to your calendar')).toBeNull();
  });
});

describe('a finished item that is still listed', () => {
  it('can still be taken off, even though it could not be added', async () => {
    await open({ state: 'done', on_calendar: true, calendar_sync_state: 'synced' });

    // The tick takes a completed item's event down on its own (D54), but until
    // it has, the control has to be reachable rather than vanish mid-removal.
    expect(screen.getByLabelText('Remove this from your calendar')).toBeTruthy();
  });
});
