/**
 * Item detail — correct it, move it, or delete it (UC37, UC38, UC21, UC39).
 *
 * The screen exists because a parse will sometimes be wrong, and until now
 * there was nowhere to say so. It shows the transcript beside the editable
 * description on purpose: the transcript is what was actually said (D14), and
 * without it a bad description is a mystery rather than a typo.
 *
 * The state chips are the one place the user sets state by hand (UC21). That
 * is deliberately a detail-screen action and not a swipe on `Today` — the
 * system is supposed to decide state from behaviour (D2), and the manual move
 * is the escape hatch, not the main road.
 *
 * Two of the moves are not ordinary chips. **Reactivate** (UC20) is the
 * counterweight to decay being silent: the system shelves and drops things
 * without saying so, so the way back has to be obvious rather than one chip
 * among four. **Snooze** (UC17) is here as well as on the notification,
 * because "not now" is an answer you also give while looking at the thing.
 *
 * **The calendar** (UC43) is here because it is a decision now, not a rule.
 * Every timed item used to sync, which buried three real appointments under
 * thirty reminders; adding one is a press, and so is taking it back off (D59).
 *
 * **People** (UC45) is here for the same reason the rest of the screen is.
 * Every capture is scanned for who it names now, not just `person_note`s, so
 * a task can carry a person — and a wider net misses in both directions: a
 * name said too quietly to hear, and a "Pansy" who turns out to be a cat. Both
 * repairs are on the row that is wrong rather than on the person's page, which
 * is where you are when you notice. Per D45, correctable beats correct.
 */
import { useCallback, useEffect, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  ScrollView,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Stack, router, useLocalSearchParams } from 'expo-router';
import DateTimePicker from '@react-native-community/datetimepicker';

import {
  ApiError,
  addItemPerson,
  addToCalendar,
  deleteItem,
  editItem,
  item as fetchItem,
  reactivateItem,
  removeFromCalendar,
  removeItemPerson,
  setItemState,
  snoozeItem,
} from '../../lib/api';
import type { ItemDetail, ItemState, LinkedPerson } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { publishItemChange } from '../../lib/itemEvents';
import { PersonPicker } from '../../lib/PersonPicker';
import { usePlayback } from '../../lib/playback';
import { capturedLabel, fullDueLabel } from '../../lib/time';
import { color, radius, space } from '../../lib/theme';

const STATES: { value: ItemState; label: string }[] = [
  { value: 'active', label: 'Active' },
  { value: 'shelved', label: 'Shelved' },
  { value: 'done', label: 'Done' },
  { value: 'dropped', label: 'Dropped' },
];

/** How the transcription provenance reads to a person (UC42, D22). */
function provenance(detail: ItemDetail): string | null {
  if (detail.source !== 'voice') return null;
  if (detail.transcript_source === 'none') {
    return 'The recording could not be transcribed. Your audio is kept.';
  }
  if (detail.parse_status === 'needs_review') {
    return 'The words were hard to make out — worth checking against the audio.';
  }
  return null;
}

/**
 * Whether this item could be on the calendar at all (UC43, D59).
 *
 * The same expression the server's `calendar_wanted` is: a time to show, and
 * a state that has not ended. Mirrored here only to decide whether the block
 * is drawn — the server refuses on its own, and it is the one that is right.
 */
function calendarPossible(detail: ItemDetail): boolean {
  return (
    detail.due_at !== null &&
    (detail.state === 'active' || detail.state === 'shelved')
  );
}

/** What the calendar block says under its button. */
function calendarHint(detail: ItemDetail): string {
  if (!detail.on_calendar) {
    return 'A time is what makes this remind you. It goes on your calendar only if you put it there.';
  }
  if (detail.calendar_stalled) {
    return 'Your calendar could not be reached, so nothing is on it yet. Try again when you have signal.';
  }
  if (detail.calendar_sync_state === 'synced') {
    return 'On your calendar. Edits follow it there, and finishing this takes it down.';
  }
  if (detail.calendar_sync_state === 'error') {
    return 'That did not get through. It tries again on its own.';
  }
  return 'Going on your calendar within the minute.';
}

export default function ItemScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { signOut } = useAuth();
  const playback = usePlayback();

  const [detail, setDetail] = useState<ItemDetail | null>(null);
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [picking, setPicking] = useState<'date' | 'time' | null>(null);
  /**
   * The date the first leg of the picker settled, waiting for the second.
   *
   * Changing the date asks for the time as well (D61), so the answer has to
   * survive between the two pickers. Null means whatever is on screen is a
   * single question — the `Time` chip on its own, or nothing.
   */
  const [pendingDate, setPendingDate] = useState<Date | null>(null);
  /** The person sheet is open (UC45). */
  const [linking, setLinking] = useState(false);

  /** Shared failure handling: sign out on 401, otherwise say what happened. */
  const failed = useCallback(
    async (e: unknown, fallback: string) => {
      if (e instanceof ApiError && e.isAuthError) {
        await signOut();
        return;
      }
      setError(
        e instanceof ApiError && e.kind === 'client'
          ? `${fallback} ${e.diagnostic}`
          : e instanceof ApiError
            ? `${fallback} ${e.message}`
            : fallback,
      );
    },
    [signOut],
  );

  const load = useCallback(async () => {
    try {
      const loaded = await fetchItem(id);
      setDetail(loaded);
      setText(loaded.text);
    } catch (e) {
      await failed(e, 'Could not load this item.');
    } finally {
      setLoading(false);
    }
  }, [id, failed]);

  useEffect(() => {
    void load();
  }, [load]);

  /** Apply an edit and fold the result back in, announcing any state move. */
  const apply = useCallback(
    async (changes: { text?: string; due_at?: string | null }, said: string) => {
      if (!detail || busy) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const before = detail.state;
        const updated = await editItem(id, changes);
        setDetail(updated);
        setText(updated.text);
        publishItemChange({ type: 'updated', id, item: updated });
        // Every state change is announced, including the ones an edit causes.
        setNotice(
          updated.state === before
            ? said
            : `${said} Moved to ${updated.state}.`,
        );
      } catch (e) {
        await failed(e, 'Could not save that change.');
      } finally {
        setBusy(false);
      }
    },
    [detail, busy, id, failed],
  );

  const saveText = useCallback(() => {
    const next = text.trim();
    if (!detail || !next || next === detail.text) return;
    void apply({ text: next }, 'Saved.');
  }, [text, detail, apply]);

  /** Take it back off the shelf, and put a time on it (UC20). */
  const reactivate = useCallback(async () => {
    if (!detail || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await reactivateItem(id);
      // Reload rather than patch: the server decides the due time on the way
      // back, and guessing it here is how the screen starts lying.
      const updated = await fetchItem(id);
      setDetail(updated);
      setText(updated.text);
      publishItemChange({ type: 'updated', id, item: updated });
      setNotice(
        result.changed
          ? `Back on Today — due ${fullDueLabel(updated.due_at)}.`
          : 'Already active.',
      );
    } catch (e) {
      await failed(e, 'Could not reactivate this item.');
    } finally {
      setBusy(false);
    }
  }, [detail, busy, id, failed]);

  /** Not now (UC17). Counts toward decay exactly as ignoring it would. */
  const snooze = useCallback(async () => {
    if (!detail || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await snoozeItem(id);
      if (!result.changed) {
        // It moved under us — the lists are showing the old state too.
        const moved = { ...detail, state: result.state };
        setDetail(moved);
        publishItemChange({ type: 'updated', id, item: moved });
        setNotice(`That one has moved — it is ${result.state} now.`);
        return;
      }
      const snoozed: ItemDetail = {
        ...detail,
        due_at: result.due_at,
        state: 'active',
      };
      setDetail(snoozed);
      publishItemChange({ type: 'updated', id, item: snoozed });
      setNotice(`Snoozed until ${fullDueLabel(result.due_at)}.`);
    } catch (e) {
      await failed(e, 'Could not snooze this item.');
    } finally {
      setBusy(false);
    }
  }, [detail, busy, id, failed]);

  const move = useCallback(
    async (state: ItemState) => {
      if (!detail || busy || detail.state === state) return;
      // Coming back to `active` is reactivation whichever control asked for
      // it, and reactivation is the one move that also has to settle a due
      // time — an active item without one is invisible everywhere (D17).
      if (state === 'active') return reactivate();
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const result = await setItemState(id, state);
        const moved = { ...detail, state: result.state };
        setDetail(moved);
        publishItemChange({ type: 'updated', id, item: moved });
        setNotice(`Moved to ${result.state}.`);
      } catch (e) {
        await failed(e, 'Could not move this item.');
      } finally {
        setBusy(false);
      }
    },
    [detail, busy, id, failed, reactivate],
  );

  /** Say who this is about, when the parse did not hear them (UC45). */
  const addPerson = useCallback(
    async (choice: { id: string } | { name: string }) => {
      setLinking(false);
      if (!detail || busy) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const result = await addItemPerson(id, choice);
        setDetail({ ...detail, people: result.people });
        const added = result.people.find((p) =>
          'id' in choice ? p.id === choice.id : p.name === choice.name.trim(),
        );
        // Their page is a list this item was not on a moment ago, and nothing
        // local can place it there — the page is ordered by capture time and
        // paged, so it reloads rather than guesses.
        if (added) publishItemChange({ type: 'linked', id, entityId: added.id });
        setNotice(
          result.changed
            ? `Filed under ${added?.name ?? 'them'} as well.`
            : 'That one was already linked.',
        );
      } catch (e) {
        await failed(e, 'Could not link that person.');
      } finally {
        setBusy(false);
      }
    },
    [detail, busy, id, failed],
  );

  /**
   * Take it off somebody's page. Nothing said is deleted (UC45, D45).
   *
   * No confirmation, in either case. The item, its words and its recording all
   * survive, and linking back undoes it. Emptying somebody also discards the
   * names they went by, which does *not* undo — that was worth a dialog for
   * two days, and answering it every time cost more than the names are worth
   * (D60). The toast is where it is mentioned instead.
   */
  const removePerson = useCallback(
    async (who: LinkedPerson) => {
      if (!detail || busy) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const result = await removeItemPerson(id, who.id);
        setDetail({ ...detail, people: result.people });
        publishItemChange({ type: 'unlinked', id, entityId: who.id });
        setNotice(
          result.person_removed
            ? `Off ${who.name}'s page — that was the last thing on it, so they and any other names they went by are gone too.`
            : `Off ${who.name}'s page. The words are untouched.`,
        );
      } catch (e) {
        await failed(e, 'Could not remove that link.');
      } finally {
        setBusy(false);
      }
    },
    [detail, busy, id, failed],
  );

  /**
   * Put this on the calendar, because it is an appointment (UC43, D59).
   *
   * Nothing waits for Google: the server writes the decision down and the tick
   * has a minute to make it true (D53). Pressing it on something already there
   * is the retry for a sync that gave up, which is why the notice distinguishes
   * the two.
   */
  const putOnCalendar = useCallback(async () => {
    if (!detail || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await addToCalendar(id);
      setDetail({
        ...detail,
        on_calendar: true,
        calendar_sync_state: result.sync_state,
        calendar_stalled: false,
      });
      setNotice(
        result.changed
          ? 'Added — it appears on your calendar within the minute.'
          : 'Already on your calendar. Trying the sync again.',
      );
    } catch (e) {
      await failed(e, 'Could not add this to your calendar.');
    } finally {
      setBusy(false);
    }
  }, [detail, busy, id, failed]);

  /** Take it back off. The item keeps its time, its state and its push. */
  const takeOffCalendar = useCallback(async () => {
    if (!detail || busy) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const result = await removeFromCalendar(id);
      setDetail({
        ...detail,
        on_calendar: false,
        calendar_sync_state: null,
        calendar_stalled: false,
      });
      setNotice(
        result.queued
          ? 'Off your calendar — the event comes down within the minute.'
          : 'Off your calendar. It still reminds you at its time.',
      );
    } catch (e) {
      await failed(e, 'Could not take this off your calendar.');
    } finally {
      setBusy(false);
    }
  }, [detail, busy, id, failed]);

  const confirmDelete = useCallback(() => {
    if (!detail) return;
    Alert.alert(
      'Delete this item?',
      detail.has_audio
        ? 'The item and its recording are removed permanently. This cannot be undone.'
        : 'This cannot be undone.',
      [
        { text: 'Keep', style: 'cancel' },
        {
          text: 'Delete',
          style: 'destructive',
          onPress: () => {
            void (async () => {
              setBusy(true);
              try {
                playback.stop();
                await deleteItem(id);
                // Back to the list, and tell it the row is gone. `Today`
                // refetches on focus and would have found out anyway; the
                // Shelf does not, by design, and used to keep showing a
                // deleted item until it was pulled to refresh.
                publishItemChange({ type: 'deleted', id });
                router.back();
              } catch (e) {
                setBusy(false);
                await failed(e, 'Could not delete this item.');
              }
            })();
          },
        },
      ],
    );
  }, [detail, id, playback, failed]);

  /**
   * Merge a picked date or time into the due moment, asking for both (D61).
   *
   * A date answered on its own used to keep the old time of day, which is
   * almost never what was meant: moving something to Thursday moves it to a
   * different part of the day too. So the date leg hands over to the time
   * leg, and one edit is sent when both have been answered.
   *
   * Dismissing either leg abandons the whole change. A cancel is a cancel —
   * writing the date alone is the behaviour this exists to stop.
   */
  const onPicked = useCallback(
    (picked: Date | undefined) => {
      const mode = picking;
      const carried = pendingDate;
      setPicking(null);
      if (!picked || !detail) {
        setPendingDate(null);
        return;
      }

      // The date leg's answer if there is one, so the time leg edits the day
      // that was just chosen rather than the day the item came in with.
      const base = carried ?? (detail.due_at ? new Date(detail.due_at) : new Date());
      const next = new Date(base);

      if (mode === 'date') {
        next.setFullYear(picked.getFullYear(), picked.getMonth(), picked.getDate());
        setPendingDate(next);
        setPicking('time');
        return;
      }

      next.setHours(picked.getHours(), picked.getMinutes(), 0, 0);
      setPendingDate(null);
      void apply(
        { due_at: next.toISOString() },
        carried ? 'Due date and time updated.' : 'Time updated.',
      );
    },
    [picking, pendingDate, detail, apply],
  );

  if (loading) {
    return (
      <SafeAreaView style={styles.centered}>
        <ActivityIndicator color={color.muted} />
      </SafeAreaView>
    );
  }

  if (!detail) {
    return (
      <SafeAreaView style={styles.centered}>
        <Text style={styles.error}>{error ?? 'That item is gone.'}</Text>
        <Pressable onPress={() => router.back()} style={styles.backLink}>
          <Text style={styles.backLinkText}>Back</Text>
        </Pressable>
      </SafeAreaView>
    );
  }

  const note = provenance(detail);
  const playing = playback.activeId === detail.id && !playback.loading;

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right']}>
      <Stack.Screen options={{ title: 'Item' }} />
      <KeyboardAvoidingView
        style={styles.fill}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.header}>
          <Pressable accessibilityRole="button" hitSlop={12} onPress={() => router.back()}>
            <Text style={styles.back}>Back</Text>
          </Pressable>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Delete this item"
            hitSlop={12}
            disabled={busy}
            onPress={confirmDelete}
          >
            <Text style={[styles.delete, busy && styles.dimmed]}>Delete</Text>
          </Pressable>
        </View>

        <ScrollView
          style={styles.fill}
          contentContainerStyle={styles.body}
          keyboardShouldPersistTaps="handled"
        >
          <Text style={styles.label}>Description</Text>
          <TextInput
            style={styles.textInput}
            value={text}
            onChangeText={setText}
            onBlur={saveText}
            multiline
            editable={!busy}
            accessibilityLabel="Item description"
            placeholder="What is this?"
            placeholderTextColor={color.faint}
          />
          {text.trim() !== detail.text && text.trim() ? (
            <Pressable
              accessibilityRole="button"
              onPress={saveText}
              disabled={busy}
              style={styles.saveButton}
            >
              <Text style={styles.saveLabel}>Save</Text>
            </Pressable>
          ) : null}

          <Text style={styles.label}>Due</Text>
          <View style={styles.dueRow}>
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Change the due date and time"
              disabled={busy}
              onPress={() => setPicking('date')}
              style={styles.chip}
            >
              <Text style={styles.chipText}>{fullDueLabel(detail.due_at)}</Text>
            </Pressable>
            {detail.due_at ? (
              <>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Change the due time"
                  disabled={busy}
                  onPress={() => setPicking('time')}
                  style={styles.chip}
                >
                  <Text style={styles.chipText}>Time</Text>
                </Pressable>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel="Clear the due time"
                  disabled={busy}
                  onPress={() => void apply({ due_at: null }, 'Time cleared.')}
                  style={styles.chip}
                >
                  <Text style={styles.chipText}>Clear</Text>
                </Pressable>
              </>
            ) : null}
          </View>

          {picking ? (
            <DateTimePicker
              value={
                pendingDate ?? (detail.due_at ? new Date(detail.due_at) : new Date())
              }
              mode={picking}
              onChange={(_event, picked) => onPicked(picked)}
            />
          ) : null}

          {calendarPossible(detail) || detail.on_calendar ? (
            <>
              <Text style={styles.label}>Calendar</Text>
              <View style={styles.chipRow}>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={
                    detail.on_calendar
                      ? 'Remove this from your calendar'
                      : 'Add this to your calendar'
                  }
                  disabled={busy}
                  onPress={() =>
                    void (detail.on_calendar ? takeOffCalendar() : putOnCalendar())
                  }
                  style={[styles.chip, busy && styles.dimmed]}
                >
                  <Text style={styles.chipText}>
                    {detail.on_calendar ? 'Remove from calendar' : 'Add to calendar'}
                  </Text>
                </Pressable>
                {/* The only way back for a link that spent its attempts. The
                    button beside it says Remove by then, so without this the
                    item is stuck listed-but-absent until it is edited. */}
                {detail.on_calendar && detail.calendar_stalled ? (
                  <Pressable
                    accessibilityRole="button"
                    accessibilityLabel="Try adding it to your calendar again"
                    disabled={busy}
                    onPress={() => void putOnCalendar()}
                    style={[styles.chip, busy && styles.dimmed]}
                  >
                    <Text style={styles.chipText}>Try again</Text>
                  </Pressable>
                ) : null}
              </View>
              <Text style={styles.hint}>{calendarHint(detail)}</Text>
            </>
          ) : null}

          {detail.state === 'shelved' || detail.state === 'dropped' ? (
            <>
              <Text style={styles.label}>On the shelf</Text>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Reactivate this item"
                disabled={busy}
                onPress={() => void reactivate()}
                style={({ pressed }) => [
                  styles.primaryButton,
                  pressed && styles.pressed,
                  busy && styles.dimmed,
                ]}
              >
                <Text style={styles.primaryLabel}>Put it back on Today</Text>
              </Pressable>
              <Text style={styles.hint}>
                {detail.state === 'shelved'
                  ? 'Shelved items are not due and do not remind you.'
                  : 'Dropped items are out of the way, not gone.'}
              </Text>
            </>
          ) : null}

          {detail.state === 'active' && detail.due_at ? (
            <>
              <Text style={styles.label}>Not now</Text>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Snooze this item"
                disabled={busy}
                onPress={() => void snooze()}
                style={({ pressed }) => [
                  styles.secondaryButton,
                  pressed && styles.pressed,
                  busy && styles.dimmed,
                ]}
              >
                <Text style={styles.secondaryLabel}>Snooze</Text>
              </Pressable>
              <Text style={styles.hint}>
                Snoozing counts the same as ignoring it. Enough of either and
                it goes to the shelf on its own.
              </Text>
            </>
          ) : null}

          <Text style={styles.label}>State</Text>
          <View style={styles.chipRow}>
            {STATES.map(({ value, label }) => {
              const current = detail.state === value;
              return (
                <Pressable
                  key={value}
                  accessibilityRole="button"
                  accessibilityLabel={`Move to ${label}`}
                  accessibilityState={{ selected: current }}
                  disabled={busy || current}
                  onPress={() => void move(value)}
                  style={[styles.chip, current && styles.chipActive]}
                >
                  <Text style={[styles.chipText, current && styles.chipTextActive]}>
                    {label}
                  </Text>
                </Pressable>
              );
            })}
          </View>

          <Text style={styles.label}>People</Text>
          <View style={styles.chipRow}>
            {detail.people.map((who) => (
              <View key={who.id} style={styles.personChip}>
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={`Open ${who.name}`}
                  disabled={busy}
                  onPress={() => router.push(`/person/${who.id}`)}
                  hitSlop={6}
                >
                  <Text style={styles.personName}>{who.name}</Text>
                </Pressable>
                {/* Its own target, with room around it: a chip that both
                    navigates and unlinks would do neither reliably. */}
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={`Not about ${who.name}`}
                  disabled={busy}
                  hitSlop={10}
                  onPress={() => void removePerson(who)}
                >
                  <Text style={styles.personRemove}>×</Text>
                </Pressable>
              </View>
            ))}
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Link someone to this item"
              disabled={busy}
              onPress={() => setLinking(true)}
              style={[styles.chip, busy && styles.dimmed]}
            >
              <Text style={styles.chipText}>+ Someone</Text>
            </Pressable>
          </View>
          {detail.people.length === 0 ? (
            <Text style={styles.hint}>
              Nobody heard in this one. Adding them here puts it on their page
              too.
            </Text>
          ) : null}

          <Text style={styles.label}>What you said</Text>
          <Text style={styles.transcript}>{detail.raw_text || '—'}</Text>
          {note ? <Text style={styles.note}>{note}</Text> : null}

          {detail.has_audio ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel={playing ? 'Stop the recording' : 'Play the recording'}
              onPress={() => void playback.toggle(detail.id)}
              style={styles.playButton}
            >
              {playback.activeId === detail.id && playback.loading ? (
                <ActivityIndicator color={color.text} />
              ) : (
                <Text style={styles.playLabel}>
                  {playing ? '■  Stop' : '▶  Play the recording'}
                </Text>
              )}
            </Pressable>
          ) : null}

          <Text style={styles.footer}>
            {detail.kind} · captured {capturedLabel(detail.created_at)}
            {detail.source === 'voice' ? ' by voice' : ''}
          </Text>
        </ScrollView>

        <View style={styles.messages}>
          {error ?? playback.error ? (
            <Text style={styles.error}>{error ?? playback.error}</Text>
          ) : notice ? (
            <Text style={styles.notice}>{notice}</Text>
          ) : null}
        </View>
      </KeyboardAvoidingView>

      <PersonPicker
        visible={linking}
        title="Who is this about?"
        subtitle="It stays where it is and appears on their page as well."
        allowCreate
        onPick={(choice) => void addPerson(choice)}
        onCancel={() => setLinking(false)}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
  // Reactivate is the only filled button on this screen. Decay is silent, so
  // the way back out of it should not need looking for (UC20).
  primaryButton: {
    backgroundColor: color.accent,
    borderRadius: radius.md,
    paddingVertical: 14,
    alignItems: 'center',
  },
  primaryLabel: { fontSize: 16, fontWeight: '600', color: color.accentText },
  secondaryButton: {
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    backgroundColor: color.surface,
    paddingVertical: 12,
    alignItems: 'center',
  },
  secondaryLabel: { fontSize: 15, fontWeight: '600', color: color.text },
  pressed: { opacity: 0.7 },
  hint: { fontSize: 13, lineHeight: 19, color: color.muted },
  centered: {
    flex: 1,
    backgroundColor: color.bg,
    alignItems: 'center',
    justifyContent: 'center',
    gap: space.md,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
    paddingBottom: space.md,
  },
  back: { fontSize: 16, color: color.accent, fontWeight: '600' },
  backLink: { padding: space.sm },
  backLinkText: { fontSize: 16, color: color.accent, fontWeight: '600' },
  delete: { fontSize: 16, color: color.danger, fontWeight: '600' },
  dimmed: { opacity: 0.4 },
  body: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.sm },
  label: {
    marginTop: space.md,
    fontSize: 12,
    fontWeight: '700',
    letterSpacing: 0.6,
    textTransform: 'uppercase',
    color: color.faint,
  },
  textInput: {
    fontSize: 20,
    lineHeight: 28,
    color: color.text,
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    padding: space.md,
    minHeight: 72,
  },
  saveButton: {
    alignSelf: 'flex-start',
    backgroundColor: color.accent,
    borderRadius: radius.pill,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  saveLabel: { color: color.accentText, fontSize: 15, fontWeight: '600' },
  dueRow: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  chipRow: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  chip: {
    backgroundColor: color.surface,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  chipActive: { backgroundColor: color.accent, borderColor: color.accent },
  // A person reads as a name, not as a state: bordered like the other chips so
  // the row is one row, but the name carries the accent because it is a link
  // somewhere rather than a setting.
  personChip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    backgroundColor: color.surface,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
    paddingLeft: space.md,
    paddingRight: space.sm + 2,
    paddingVertical: space.sm,
  },
  personName: { fontSize: 14, color: color.accent, fontWeight: '600' },
  personRemove: { fontSize: 16, lineHeight: 18, color: color.faint },
  chipText: { fontSize: 14, color: color.text },
  chipTextActive: { color: color.accentText, fontWeight: '600' },
  transcript: {
    fontSize: 16,
    lineHeight: 24,
    color: color.muted,
    fontStyle: 'italic',
  },
  note: { fontSize: 14, lineHeight: 20, color: color.overdue },
  playButton: {
    marginTop: space.sm,
    alignSelf: 'flex-start',
    backgroundColor: color.surface,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
    paddingHorizontal: space.lg,
    paddingVertical: space.sm,
  },
  playLabel: { fontSize: 15, color: color.text },
  footer: { marginTop: space.lg, fontSize: 13, color: color.faint },
  messages: {
    minHeight: 44,
    justifyContent: 'center',
    paddingHorizontal: space.lg,
    paddingBottom: space.sm,
  },
  notice: { fontSize: 14, lineHeight: 20, color: color.muted },
  error: { fontSize: 14, lineHeight: 20, color: color.danger },
});
