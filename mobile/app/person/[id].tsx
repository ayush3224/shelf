/**
 * A person page (UC46) — everything ever said about somebody.
 *
 * Newest first. `docs/use-cases.md` originally wrote UC46 as "oldest to
 * newest"; the owner asked for the reverse on 24 August 2026 and the doc has
 * been amended. The reason holds: you open this to remember where things
 * stand, and oldest-first buries that under the history every single time.
 *
 * Every state is here — `done` and `dropped` included. A page that showed only
 * what was outstanding would be answering "what do I owe this person", which is
 * a different and much narrower question than "what do I know here". It is also
 * the question the rest of the app already answers.
 *
 * Playback is on the row (UC7), because half of what gets said about a person
 * is said rather than typed, and the transcript is a lossy copy of it.
 *
 * **This is also where a wrong resolution gets corrected** (UC48, UC49). Merge
 * folds another person into this one; select-and-move sends notes to somebody
 * else. Both are two taps from here, and between them any automatic mistake is
 * recoverable — which is precisely what lets the resolution rules stay willing
 * to guess (D45).
 *
 * Only the merge asks first. It is the one that removes a row; a move relocates
 * mentions and can be undone by moving them back, so a dialog in front of it
 * would be ceremony rather than a safeguard.
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Alert,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Stack, router, useLocalSearchParams } from 'expo-router';

import {
  ApiError,
  mergePerson,
  person as fetchPerson,
  splitPerson,
} from '../../lib/api';
import type { ItemState, Person, PersonItem } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { PersonPicker } from '../../lib/PersonPicker';
import { usePlayback } from '../../lib/playback';
import { capturedOnLabel } from '../../lib/time';
import { color, radius, space } from '../../lib/theme';

/** Plain words. Nothing on a person page is an alarm. */
const STATE_WORD: Record<ItemState, string> = {
  active: 'Active',
  shelved: 'Shelved',
  done: 'Done',
  dropped: 'Dropped',
};

type RowProps = {
  item: PersonItem;
  onOpen: (id: string) => void;
  onPlay: (id: string) => void;
  playing: boolean;
  loadingAudio: boolean;
  /** Selection mode is on; the row picks rather than navigates. */
  selecting: boolean;
  selected: boolean;
  onToggle: (id: string) => void;
};

function Row({
  item,
  onOpen,
  onPlay,
  playing,
  loadingAudio,
  selecting,
  selected,
  onToggle,
}: RowProps) {
  return (
    <View style={[styles.row, selecting && selected && styles.rowSelected]}>
      {selecting ? (
        <Pressable
          accessibilityRole="checkbox"
          accessibilityState={{ checked: selected }}
          accessibilityLabel={`Select: ${item.text}`}
          hitSlop={10}
          onPress={() => onToggle(item.id)}
          style={[styles.check, selected && styles.checkOn]}
        >
          {selected ? <Text style={styles.checkGlyph}>✓</Text> : null}
        </Pressable>
      ) : null}

      <Pressable
        accessibilityRole="button"
        accessibilityLabel={
          selecting ? `Select: ${item.text}` : `Open: ${item.text}`
        }
        onPress={() => (selecting ? onToggle(item.id) : onOpen(item.id))}
        style={({ pressed }) => [styles.rowBody, pressed && styles.rowPressed]}
      >
        <Text style={styles.rowText}>{item.text}</Text>
        <View style={styles.meta}>
          <Text style={styles.metaText}>{capturedOnLabel(item.created_at)}</Text>
          <Text style={styles.metaDot}>·</Text>
          <Text style={styles.metaText}>{STATE_WORD[item.state]}</Text>
          {item.parse_status !== 'ok' ? (
            <>
              <Text style={styles.metaDot}>·</Text>
              <Text style={styles.metaText}>Check this</Text>
            </>
          ) : null}
        </View>
      </Pressable>

      {item.has_audio && !selecting ? (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={playing ? 'Stop the recording' : 'Play the recording'}
          hitSlop={10}
          onPress={() => onPlay(item.id)}
          style={({ pressed }) => [styles.play, pressed && styles.playPressed]}
        >
          {loadingAudio ? (
            <ActivityIndicator color={color.muted} size="small" />
          ) : (
            <Text style={styles.playGlyph}>{playing ? '■' : '▶'}</Text>
          )}
        </Pressable>
      ) : null}
    </View>
  );
}

export default function PersonScreen() {
  const { id } = useLocalSearchParams<{ id: string }>();
  const { signOut } = useAuth();
  const playback = usePlayback();

  const [who, setWho] = useState<Person | null>(null);
  const [items, setItems] = useState<PersonItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paging, setPaging] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  /** Selection mode, and what is selected. Empty set means mode is off. */
  const [selection, setSelection] = useState<Set<string> | null>(null);
  /** Which flow the picker is serving, or null when it is closed. */
  const [picking, setPicking] = useState<'merge' | 'move' | null>(null);

  const generation = useRef(0);

  const failed = useCallback(
    async (e: unknown, fallback: string) => {
      if (e instanceof ApiError && e.isAuthError) {
        await signOut();
        return;
      }
      setError(e instanceof ApiError ? e.message : fallback);
    },
    [signOut],
  );

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const mine = ++generation.current;
      if (mode === 'refresh') setRefreshing(true);
      setError(null);
      try {
        const page = await fetchPerson(id);
        if (generation.current !== mine) return;
        setWho(page.person);
        setItems(page.items);
        setCursor(page.has_more ? page.next_cursor : null);
      } catch (e) {
        if (generation.current !== mine) return;
        await failed(e, 'Could not load this person.');
      } finally {
        if (generation.current === mine) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [id, failed],
  );

  const more = useCallback(async () => {
    if (!cursor || paging || loading) return;
    const mine = generation.current;
    setPaging(true);
    try {
      const page = await fetchPerson(id, cursor);
      if (generation.current !== mine) return;
      setItems((current) => [...current, ...page.items]);
      setCursor(page.has_more ? page.next_cursor : null);
    } catch (e) {
      if (generation.current !== mine) return;
      await failed(e, 'Could not load more.');
    } finally {
      setPaging(false);
    }
  }, [cursor, paging, loading, id, failed]);

  useEffect(() => {
    void load('initial');
  }, [load]);


  const selecting = selection !== null;

  const toggle = useCallback((itemId: string) => {
    setSelection((current) => {
      if (current === null) return current;
      const next = new Set(current);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  }, []);

  const endSelection = useCallback(() => {
    setSelection(null);
    setPicking(null);
  }, []);

  /** Fold somebody into this person (UC48). Destructive, so it asks first. */
  const merge = useCallback(
    async (absorbId: string) => {
      setPicking(null);
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const result = await mergePerson(id, absorbId);
        setWho(result.person);
        setNotice(
          `${result.absorbed_name} folded in — ${result.moved} ${
            result.moved === 1 ? 'note' : 'notes'
          } moved here.`,
        );
        // Reload rather than patch: the merged list is a different page of a
        // different length, and guessing it here is how the screen starts lying.
        await load('refresh');
      } catch (e) {
        await failed(e, 'Could not merge those two.');
      } finally {
        setBusy(false);
      }
    },
    [id, load, failed],
  );

  const confirmMerge = useCallback(
    (choice: { id: string } | { name: string }) => {
      if (!('id' in choice)) return;
      const absorbed = choice.id;
      // The only dialog in either flow. A merge removes a row; a move does not.
      Alert.alert(
        'Fold them together?',
        `Their notes move to ${who?.name ?? 'this person'}, their name becomes another name for ${who?.name ?? 'them'}, and their entry is removed. The notes are not deleted.`,
        [
          { text: 'Cancel', style: 'cancel', onPress: () => setPicking(null) },
          {
            text: 'Merge',
            style: 'destructive',
            onPress: () => void merge(absorbed),
          },
        ],
      );
    },
    [who, merge],
  );

  /** Move the selected notes to somebody else (UC49). Nothing is deleted. */
  const move = useCallback(
    async (choice: { id: string } | { name: string }) => {
      const chosen = selection ? [...selection] : [];
      setPicking(null);
      if (!chosen.length) return;
      setBusy(true);
      setError(null);
      setNotice(null);
      try {
        const result = await splitPerson(id, chosen, choice);
        setSelection(null);
        if (result.source_removed) {
          // Every note left. The page we are on no longer exists, so staying
          // on it would render a 404 the moment anything refetched.
          router.replace(`/person/${result.target.id}`);
          return;
        }
        const alias = result.aliases_moved.length
          ? ` “${result.aliases_moved.join('”, “')}” went with them.`
          : '';
        setNotice(
          `${result.moved} ${result.moved === 1 ? 'note' : 'notes'} moved to ${result.target.name}.${alias}`,
        );
        await load('refresh');
      } catch (e) {
        await failed(e, 'Could not move those notes.');
      } finally {
        setBusy(false);
      }
    },
    [id, selection, load, failed],
  );

  if (loading) {
    return (
      <SafeAreaView style={styles.centered} edges={['top', 'left', 'right']}>
        <Stack.Screen options={{ headerShown: false }} />
        <ActivityIndicator color={color.muted} />
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right']}>
      <Stack.Screen options={{ headerShown: false }} />

      <View style={styles.header}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={selecting ? 'Stop selecting' : 'Back to people'}
          hitSlop={12}
          onPress={() => (selecting ? endSelection() : router.back())}
        >
          <Text style={styles.back}>{selecting ? 'Cancel' : '‹ People'}</Text>
        </Pressable>
        <Text style={styles.title}>{who?.name ?? 'Person'}</Text>
        <View style={styles.subhead}>
          {who?.aliases.length ? (
            <Text style={styles.aka}>also {who.aliases.join(', ')}</Text>
          ) : null}
          <Text style={styles.aka}>
            {who?.mentions === 1 ? '1 mention' : `${who?.mentions ?? 0} mentions`}
          </Text>
        </View>

        {/* Two taps to either correction, and never more than two controls in
            view: the actions swap for the selection's own when it is running. */}
        <View style={styles.actions}>
          {selecting ? (
            <>
              <Text style={styles.selectedCount}>
                {selection?.size ?? 0} selected
              </Text>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Move the selected notes to someone else"
                disabled={busy || !selection?.size}
                onPress={() => setPicking('move')}
                style={({ pressed }) => [
                  styles.action,
                  styles.actionPrimary,
                  pressed && styles.pressed,
                  (busy || !selection?.size) && styles.dimmed,
                ]}
              >
                <Text style={styles.actionPrimaryText}>Move to…</Text>
              </Pressable>
            </>
          ) : (
            <>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Select notes to move to someone else"
                disabled={busy || items.length === 0}
                onPress={() => setSelection(new Set())}
                style={({ pressed }) => [
                  styles.action,
                  pressed && styles.pressed,
                  (busy || items.length === 0) && styles.dimmed,
                ]}
              >
                <Text style={styles.actionText}>Move notes</Text>
              </Pressable>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Fold another person into this one"
                disabled={busy}
                onPress={() => setPicking('merge')}
                style={({ pressed }) => [
                  styles.action,
                  pressed && styles.pressed,
                  busy && styles.dimmed,
                ]}
              >
                <Text style={styles.actionText}>Merge</Text>
              </Pressable>
            </>
          )}
          {busy ? <ActivityIndicator color={color.muted} size="small" /> : null}
        </View>
      </View>

      {error ?? playback.error ? (
        <Text style={styles.error}>{error ?? playback.error}</Text>
      ) : null}
      {notice && !error ? <Text style={styles.notice}>{notice}</Text> : null}

      <FlatList
        data={items}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => (
          <Row
            item={item}
            onOpen={(itemId) => router.push(`/item/${itemId}`)}
            onPlay={(itemId) => void playback.toggle(itemId)}
            playing={playback.activeId === item.id && !playback.loading}
            loadingAudio={playback.activeId === item.id && playback.loading}
            selecting={selecting}
            selected={selection?.has(item.id) ?? false}
            onToggle={toggle}
          />
        )}
        contentContainerStyle={items.length === 0 ? styles.emptyWrap : styles.list}
        onEndReachedThreshold={0.5}
        onEndReached={() => void more()}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => void load('refresh')}
            tintColor={color.muted}
          />
        }
        ListFooterComponent={
          paging ? (
            <ActivityIndicator style={styles.footer} color={color.faint} />
          ) : null
        }
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyTitle}>Nothing yet.</Text>
            <Text style={styles.emptyBody}>
              Everything you say about {who?.name ?? 'them'} collects here.
            </Text>
          </View>
        }
      />

      <PersonPicker
        visible={picking !== null}
        title={picking === 'merge' ? 'Fold in whom?' : 'Move to whom?'}
        subtitle={
          picking === 'merge'
            ? `Their notes come here and their entry is removed. ${who?.name ?? 'This person'} stays.`
            : 'The notes move. Nothing is deleted.'
        }
        excludeId={id}
        // A merge needs somebody who already has notes; there is nothing to
        // fold in from a person who does not exist yet.
        allowCreate={picking === 'move'}
        onPick={picking === 'merge' ? confirmMerge : move}
        onCancel={() => setPicking(null)}
      />
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  centered: {
    flex: 1,
    backgroundColor: color.bg,
    alignItems: 'center',
    justifyContent: 'center',
  },
  header: {
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
    paddingBottom: space.md,
    gap: space.xs,
  },
  back: { fontSize: 15, color: color.muted, marginBottom: space.sm },
  title: { fontSize: 28, fontWeight: '700', color: color.text, letterSpacing: -0.6 },
  subhead: { flexDirection: 'row', flexWrap: 'wrap', gap: space.sm },
  aka: { fontSize: 13, color: color.faint },
  error: {
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    fontSize: 14,
    lineHeight: 20,
    color: color.danger,
  },
  notice: {
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    fontSize: 14,
    lineHeight: 20,
    color: color.muted,
  },
  actions: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    marginTop: space.sm,
  },
  selectedCount: { flex: 1, fontSize: 14, color: color.muted },
  // `minHeight` and padding, not a height: these hold text (D42).
  action: {
    minHeight: 34,
    justifyContent: 'center',
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
    backgroundColor: color.surface,
  },
  actionText: { fontSize: 13, fontWeight: '600', color: color.muted },
  actionPrimary: { backgroundColor: color.accent, borderColor: color.accent },
  actionPrimaryText: { fontSize: 13, fontWeight: '600', color: color.accentText },
  pressed: { opacity: 0.6 },
  dimmed: { opacity: 0.4 },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.sm },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.md,
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    padding: space.md,
  },
  rowPressed: { opacity: 0.6 },
  rowSelected: { borderColor: color.accent },
  check: {
    width: 22,
    height: 22,
    borderRadius: radius.pill,
    borderWidth: 2,
    borderColor: color.border,
    alignItems: 'center',
    justifyContent: 'center',
  },
  checkOn: { backgroundColor: color.accent, borderColor: color.accent },
  checkGlyph: { fontSize: 12, color: color.accentText, fontWeight: '700' },
  rowBody: { flex: 1, gap: space.xs },
  rowText: { fontSize: 16, lineHeight: 22, color: color.text },
  meta: { flexDirection: 'row', alignItems: 'center', gap: space.xs + 2 },
  metaText: { fontSize: 13, color: color.muted },
  metaDot: { fontSize: 13, color: color.faint },
  play: {
    width: 36,
    height: 36,
    borderRadius: 18,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: color.border,
  },
  playPressed: { opacity: 0.7 },
  playGlyph: { fontSize: 13, color: color.text },
  footer: { paddingVertical: space.md },
  emptyWrap: { flexGrow: 1 },
  empty: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.xl,
    gap: space.sm,
  },
  emptyTitle: { fontSize: 20, fontWeight: '600', color: color.text },
  emptyBody: {
    fontSize: 15,
    lineHeight: 22,
    color: color.muted,
    textAlign: 'center',
  },
});

export { RouteError as ErrorBoundary } from '../../lib/RouteError';
