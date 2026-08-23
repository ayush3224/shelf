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
  deleteItem,
  editItem,
  item as fetchItem,
  setItemState,
} from '../../lib/api';
import type { ItemDetail, ItemState } from '../../lib/api';
import { useAuth } from '../../lib/auth';
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

  const move = useCallback(
    async (state: ItemState) => {
      if (!detail || busy || detail.state === state) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const result = await setItemState(id, state);
        setDetail({ ...detail, state: result.state });
        setNotice(`Moved to ${result.state}.`);
      } catch (e) {
        await failed(e, 'Could not move this item.');
      } finally {
        setBusy(false);
      }
    },
    [detail, busy, id, failed],
  );

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
                // Back to the list; the row is gone, so there is nothing to
                // come back to.
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

  /** Merge a picked date or time into the existing due moment. */
  const onPicked = useCallback(
    (picked: Date | undefined) => {
      const mode = picking;
      setPicking(null);
      if (!picked || !detail) return;

      const base = detail.due_at ? new Date(detail.due_at) : new Date();
      const next = new Date(base);
      if (mode === 'date') {
        next.setFullYear(picked.getFullYear(), picked.getMonth(), picked.getDate());
      } else {
        next.setHours(picked.getHours(), picked.getMinutes(), 0, 0);
      }
      void apply({ due_at: next.toISOString() }, 'Time updated.');
    },
    [picking, detail, apply],
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
              accessibilityLabel="Change the due date"
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
              value={detail.due_at ? new Date(detail.due_at) : new Date()}
              mode={picking}
              onChange={(_event, picked) => onPicked(picked)}
            />
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
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1 },
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
