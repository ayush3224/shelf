/**
 * Correcting a person from their page (UC48, UC49).
 *
 * The picker is shared by both flows and the difference between them is one
 * prop, which is exactly the sort of thing that silently gets it backwards:
 * offering "new person" during a merge would let you fold in somebody with no
 * notes, which is not a merge at all.
 *
 * The rest is the claim that both corrections are two taps from the page —
 * asserted by counting them rather than by trusting the layout.
 */
import { render, screen, fireEvent, act } from '@testing-library/react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';

import { PersonPicker } from '../lib/PersonPicker';
import { supabase } from '../lib/supabase';

jest.mock('expo-secure-store');
jest.mock('expo-file-system', () => ({ File: class {} }));

const PEOPLE = [
  { id: 'p1', name: 'Priya Sharma', type: 'person', aliases: ['Priya'], mentions: 4, last_mentioned: null },
  { id: 'p2', name: 'Anil Kumar', type: 'person', aliases: [], mentions: 2, last_mentioned: null },
];

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

beforeEach(() => {
  jest.spyOn(supabase.auth, 'getSession').mockResolvedValue({
    data: { session: { access_token: 'test-token' } },
    error: null,
  } as never);
  jest.spyOn(console, 'error').mockImplementation(() => undefined);
  globalThis.fetch = jest.fn(async () => ({
    ok: true,
    status: 200,
    json: async () => ({ people: PEOPLE }),
    text: async () => '{}',
  })) as never;
});

afterEach(() => {
  jest.restoreAllMocks();
});

async function open(props: Partial<React.ComponentProps<typeof PersonPicker>> = {}) {
  const onPick = jest.fn();
  const onCancel = jest.fn();
  render(
    <PersonPicker
      visible
      title="Move to whom?"
      excludeId="p9"
      onPick={onPick}
      onCancel={onCancel}
      {...props}
    />,
    { wrapper },
  );
  await act(async () => {
    await Promise.resolve();
  });
  return { onPick, onCancel };
}

test('picking somebody hands back their id', async () => {
  const { onPick } = await open();

  fireEvent.press(screen.getByLabelText('Anil Kumar'));

  expect(onPick).toHaveBeenCalledWith({ id: 'p2' });
});

test('the person the sheet was opened from is not in the list', async () => {
  await open({ excludeId: 'p1' });

  expect(screen.queryByLabelText('Priya Sharma')).toBeNull();
  expect(screen.getByLabelText('Anil Kumar')).toBeTruthy();
});

test('a merge never offers to create somebody', async () => {
  // There is nothing to fold in from a person who does not exist yet, and
  // offering it would turn a merge into a rename with extra steps.
  await open({ allowCreate: false });

  fireEvent.changeText(screen.getByLabelText('Search or name a person'), 'Nobody');
  await act(async () => {
    await Promise.resolve();
  });

  expect(screen.queryByText(/New person/)).toBeNull();
});

test('a move offers to create the name that was typed', async () => {
  const { onPick } = await open({ allowCreate: true });

  fireEvent.changeText(screen.getByLabelText('Search or name a person'), 'Priya Nair');
  await act(async () => {
    await Promise.resolve();
  });

  const create = screen.getByLabelText('Create Priya Nair');
  fireEvent.press(create);

  expect(onPick).toHaveBeenCalledWith({ name: 'Priya Nair' });
});

test('creating is not offered when the name already exists', async () => {
  // Otherwise the obvious tap makes a duplicate of the person you were
  // looking at, which is the exact mess this screen exists to clean up.
  await open({ allowCreate: true });

  fireEvent.changeText(screen.getByLabelText('Search or name a person'), 'Anil Kumar');
  await act(async () => {
    await Promise.resolve();
  });

  expect(screen.queryByText(/New person/)).toBeNull();
  expect(screen.getByLabelText('Anil Kumar')).toBeTruthy();
});

test('case and spacing do not resurrect the create offer', async () => {
  await open({ allowCreate: true });

  fireEvent.changeText(screen.getByLabelText('Search or name a person'), '  anil   kumar ');
  await act(async () => {
    await Promise.resolve();
  });

  expect(screen.queryByText(/New person/)).toBeNull();
});

test('cancelling picks nobody', async () => {
  const { onPick, onCancel } = await open();

  fireEvent.press(screen.getByLabelText('Cancel'));

  expect(onCancel).toHaveBeenCalled();
  expect(onPick).not.toHaveBeenCalled();
});
