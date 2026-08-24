/**
 * The Shelf (UC33, UC34, UC36) — everything that is not `active`.
 *
 * **This screen must not read as a backlog.** `Today` is the list you are
 * meant to finish; the Shelf is the opposite of it, and the temptation with
 * "everything else" is to dress it as debt — counts, badges, red, oldest-first
 * so the guilt floats to the top. All of that is deliberately absent. Rows are
 * newest-capture-first, states are named in the same muted grey as the date,
 * nothing is coloured as urgent, and the item count only appears once you have
 * asked a question that a number answers. It is an archive of things you said,
 * not a queue of things you owe.
 *
 * That is also why decay is not called out here. UC22 was dropped, so items
 * arrive on this screen silently; the digest (UC31) is where that becomes
 * visible. Flagging "shelved 3 days ago, by the way" on every row would be
 * reinstating UC22 through the back door and would turn the archive back into
 * a ledger.
 *
 * **Reactivate** (UC20) is the one action offered on a row, and it is offered
 * quietly — a ghost button, not a call to action. Until this screen existed it
 * was reachable only from the item detail of an item you could only reach from
 * `Today`, which meant that in practice a shelved item had no way back at all.
 *
 * Paging is keyset, from the first request. This table only grows, and the
 * screen that browses all of it is the one place that has to assume so.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  SectionList,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { router, useFocusEffect } from 'expo-router';

import { ApiError, browseItems, projects, reactivateItem } from '../../lib/api';
import type { ItemState, ProjectSummary, ShelfItem } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { groupByProject, windowStart } from '../../lib/shelf';
import { capturedOnLabel } from '../../lib/time';
import { color, radius, space } from '../../lib/theme';

/** Rows per request. Small enough to paint fast, big enough not to fetch per flick. */
const PAGE = 30;

/** The server refuses a shorter search, so do not spend a request finding out. */
const MIN_SEARCH = 2;

/** Long enough that typing a word is one request, short enough to feel live. */
const SEARCH_DEBOUNCE_MS = 300;

/**
 * State chips (UC36).
 *
 * `active` is here even though the Shelf excludes it by default: once you are
 * searching, the item you are looking for may well be due today, and a search
 * that silently could not see it would be worse than no search.
 */
const STATE_CHIPS: { value: ItemState; label: string }[] = [
  { value: 'shelved', label: 'Shelved' },
  { value: 'done', label: 'Done' },
  { value: 'dropped', label: 'Dropped' },
  { value: 'active', label: 'Active' },
];

/** Date-range chips (UC36), as a window back from now rather than two pickers. */
const WHEN_CHIPS: { value: number | null; label: string }[] = [
  { value: null, label: 'Any time' },
  { value: 7, label: '7 days' },
  { value: 30, label: '30 days' },
  { value: 365, label: '12 months' },
];

/** How a state reads on a row. Plain words; nothing here is an alarm. */
const STATE_WORD: Record<ItemState, string> = {
  active: 'Active',
  shelved: 'Shelved',
  done: 'Done',
  dropped: 'Dropped',
};

type ChipProps = {
  label: string;
  selected: boolean;
  onPress: () => void;
};

function Chip({ label, selected, onPress }: ChipProps) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityState={{ selected }}
      accessibilityLabel={label}
      onPress={onPress}
      style={({ pressed }) => [
        styles.chip,
        selected && styles.chipOn,
        pressed && styles.chipPressed,
      ]}
    >
      <Text style={[styles.chipText, selected && styles.chipTextOn]}>{label}</Text>
    </Pressable>
  );
}

type RowProps = {
  item: ShelfItem;
  onOpen: (id: string) => void;
  onReactivate: (id: string) => void;
  busy: boolean;
};

function Row({ item, onOpen, onReactivate, busy }: RowProps) {
  // Reactivating a `done` item is not what UC20 is for — that is undoing a
  // completion, which is the state chips on the detail screen (UC21). This
  // button is the way back from something the *system* put away.
  const canReactivate = item.state === 'shelved' || item.state === 'dropped';

  return (
    <View style={styles.row}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Open: ${item.text}`}
        onPress={() => onOpen(item.id)}
        style={({ pressed }) => [styles.rowBody, pressed && styles.rowPressed]}
      >
        <Text style={styles.rowText} numberOfLines={3}>
          {item.text}
        </Text>
        <View style={styles.meta}>
          <Text style={styles.metaText}>{STATE_WORD[item.state]}</Text>
          <Text style={styles.metaDot}>·</Text>
          <Text style={styles.metaText}>{capturedOnLabel(item.created_at)}</Text>
          {item.parse_status !== 'ok' ? (
            <>
              <Text style={styles.metaDot}>·</Text>
              <Text style={styles.metaText}>Check this</Text>
            </>
          ) : null}
        </View>
      </Pressable>

      {canReactivate ? (
        <Pressable
          accessibilityRole="button"
          accessibilityLabel={`Reactivate: ${item.text}`}
          disabled={busy}
          hitSlop={8}
          onPress={() => onReactivate(item.id)}
          style={({ pressed }) => [
            styles.ghost,
            pressed && styles.chipPressed,
            busy && styles.dimmed,
          ]}
        >
          {busy ? (
            <ActivityIndicator color={color.muted} size="small" />
          ) : (
            <Text style={styles.ghostText}>Reactivate</Text>
          )}
        </Pressable>
      ) : null}
    </View>
  );
}

export default function Shelf() {
  const { signOut } = useAuth();

  const [term, setTerm] = useState('');
  const [search, setSearch] = useState('');
  const [states, setStates] = useState<ItemState[]>([]);
  const [days, setDays] = useState<number | null>(null);
  const [project, setProject] = useState<string | null>(null);
  const [projectList, setProjectList] = useState<ProjectSummary[]>([]);

  const [items, setItems] = useState<ShelfItem[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [paging, setPaging] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [reactivating, setReactivating] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  /**
   * Which query the answer belongs to.
   *
   * Filters change faster than the network answers, and a reply to the query
   * before last must not land in the list. Compared on arrival; a stale one is
   * dropped rather than rendered.
   */
  const generation = useRef(0);

  /** Debounce the search box so a typed word is one request, not five. */
  useEffect(() => {
    const trimmed = term.trim();
    // Below the server's floor there is nothing to ask for, and clearing the
    // box has to fall straight back to the unfiltered Shelf.
    const next = trimmed.length >= MIN_SEARCH ? trimmed : '';
    if (next === search) return;
    const timer = setTimeout(() => setSearch(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [term, search]);

  const query = useMemo(
    () => ({
      q: search || undefined,
      states,
      project: project ?? undefined,
      from: windowStart(days),
      limit: PAGE,
    }),
    [search, states, project, days],
  );

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

  /** Load the first page for the current filters, replacing whatever is shown. */
  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const mine = ++generation.current;
      if (mode === 'refresh') setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        const page = await browseItems(query);
        if (generation.current !== mine) return;
        setItems(page.items);
        setCursor(page.has_more ? page.next_cursor : null);
      } catch (e) {
        if (generation.current !== mine) return;
        await failed(e, 'Could not load the shelf.');
      } finally {
        if (generation.current === mine) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [query, failed],
  );

  /** The next page, appended. Guarded so one scroll cannot fire two requests. */
  const more = useCallback(async () => {
    if (!cursor || paging || loading) return;
    const mine = generation.current;
    setPaging(true);
    try {
      const page = await browseItems({ ...query, cursor });
      // A filter changed while this was in flight: its rows belong to a list
      // that no longer exists, and appending them would interleave two queries.
      if (generation.current !== mine) return;
      setItems((current) => [...current, ...page.items]);
      setCursor(page.has_more ? page.next_cursor : null);
    } catch (e) {
      if (generation.current !== mine) return;
      await failed(e, 'Could not load more.');
    } finally {
      setPaging(false);
    }
  }, [cursor, paging, loading, query, failed]);

  useEffect(() => {
    void load('initial');
  }, [load]);

  // The chips are drawn from this, and it changes only when a project is
  // created by hand — so it is fetched once and not refetched on focus.
  useEffect(() => {
    projects()
      .then((data) => setProjectList(data.projects))
      .catch(() => {
        // A filter that cannot be offered is not worth an error banner; the
        // list itself is unaffected and the chip row simply stays hidden.
      });
  }, []);

  // Deliberately *not* a refetch on focus, which is what `Today` does. The
  // Shelf is scrolled through, and a reload that silently resets the scroll
  // position loses the user's place. Pull to refresh instead.
  useFocusEffect(
    useCallback(() => {
      setNotice(null);
    }, []),
  );

  const toggleState = useCallback((value: ItemState) => {
    setStates((current) =>
      current.includes(value)
        ? current.filter((s) => s !== value)
        : [...current, value],
    );
  }, []);

  const reactivate = useCallback(
    async (id: string) => {
      if (reactivating) return;
      setReactivating(id);
      setError(null);
      setNotice(null);
      try {
        const result = await reactivateItem(id);
        // It is no longer on the shelf, so it leaves the list — unless the
        // filters were asking for active items too, in which case it belongs
        // here and only its state word changes.
        const shows = states.length ? states.includes('active') : Boolean(search);
        setItems((current) =>
          shows
            ? current.map((i) => (i.id === id ? { ...i, state: 'active' } : i))
            : current.filter((i) => i.id !== id),
        );
        setNotice(result.changed ? 'Back on Today.' : 'That one was already active.');
      } catch (e) {
        await failed(e, 'Could not reactivate that.');
      } finally {
        setReactivating(null);
      }
    },
    [reactivating, states, search, failed],
  );

  const sections = useMemo(() => groupByProject(items), [items]);
  const filtering = Boolean(search) || states.length > 0 || days !== null || project !== null;

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right']}>
      <View style={styles.header}>
        <Text style={styles.title}>Shelf</Text>
        {/* A count only when a question was asked. An ever-present tally of
            everything you have not done is the backlog framing this screen
            exists to avoid. */}
        {filtering && !loading ? (
          <Text style={styles.count}>
            {items.length}
            {cursor ? '+' : ''}
          </Text>
        ) : null}
      </View>

      <TextInput
        style={styles.search}
        value={term}
        onChangeText={setTerm}
        placeholder="Search everything you've said"
        placeholderTextColor={color.faint}
        accessibilityLabel="Search items"
        autoCapitalize="none"
        autoCorrect={false}
        returnKeyType="search"
        clearButtonMode="while-editing"
      />

      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        keyboardShouldPersistTaps="handled"
        style={styles.chipScroll}
        contentContainerStyle={styles.chipRow}
        testID="chip-row"
      >
        {STATE_CHIPS.map((chip) => (
          <Chip
            key={chip.value}
            label={chip.label}
            selected={states.includes(chip.value)}
            onPress={() => toggleState(chip.value)}
          />
        ))}
      </ScrollView>

      <ScrollView
        horizontal
        showsHorizontalScrollIndicator={false}
        keyboardShouldPersistTaps="handled"
        style={styles.chipScroll}
        contentContainerStyle={styles.chipRow}
        testID="chip-row"
      >
        {WHEN_CHIPS.map((chip) => (
          <Chip
            key={chip.label}
            label={chip.label}
            selected={days === chip.value}
            onPress={() => setDays(chip.value)}
          />
        ))}
      </ScrollView>

      {/* Hidden until a project exists. UC11 was dropped, so nothing fills
          `project_id` on its own and this row is normally absent — which is
          that decision showing through rather than a gap. */}
      {projectList.length > 0 ? (
        <ScrollView
          horizontal
          showsHorizontalScrollIndicator={false}
          keyboardShouldPersistTaps="handled"
          style={styles.chipScroll}
          contentContainerStyle={styles.chipRow}
          testID="chip-row"
        >
          <Chip
            label="Unsorted"
            selected={project === 'none'}
            onPress={() => setProject((p) => (p === 'none' ? null : 'none'))}
          />
          {projectList.map((p) => (
            <Chip
              key={p.id}
              label={p.name}
              selected={project === p.id}
              onPress={() => setProject((current) => (current === p.id ? null : p.id))}
            />
          ))}
        </ScrollView>
      ) : null}

      {error ? <Text style={styles.error}>{error}</Text> : null}
      {notice && !error ? <Text style={styles.notice}>{notice}</Text> : null}

      {loading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={color.muted} />
        </View>
      ) : (
        <SectionList
          sections={sections}
          keyExtractor={(item) => item.id}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          renderItem={({ item }) => (
            <Row
              item={item}
              onOpen={(id) => router.push(`/item/${id}`)}
              onReactivate={(id) => void reactivate(id)}
              busy={reactivating === item.id}
            />
          )}
          renderSectionHeader={({ section }) =>
            // One section over the whole list is not a grouping, it is a
            // caption. Shown only once there is something to tell apart.
            sections.length > 1 ? (
              <Text style={styles.sectionHeader}>{section.title}</Text>
            ) : null
          }
          contentContainerStyle={
            items.length === 0 ? styles.emptyWrap : styles.list
          }
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
              <Text style={styles.emptyTitle}>
                {filtering ? 'Nothing matches.' : 'Nothing here yet.'}
              </Text>
              <Text style={styles.emptyBody}>
                {filtering
                  ? 'Try fewer filters, or different words.'
                  : 'Anything you capture without a time waits here. It is not a list to get through.'}
              </Text>
            </View>
          }
        />
      )}
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  centered: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  header: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
    paddingBottom: space.md,
  },
  title: { fontSize: 28, fontWeight: '700', color: color.text, letterSpacing: -0.6 },
  count: { fontSize: 16, fontWeight: '600', color: color.faint },
  search: {
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    paddingHorizontal: space.md,
    paddingVertical: space.sm + 2,
    fontSize: 16,
    color: color.text,
    backgroundColor: color.surface,
    borderWidth: 1,
    borderColor: color.border,
    borderRadius: radius.md,
  },
  // A `ScrollView` defaults to `flexGrow: 1, flexShrink: 1` (RN's
  // `styles.baseHorizontal`). Three of them in a column beside a `SectionList`
  // — which is another one — means four siblings all willing to shrink, and
  // when the page's content exceeds the screen they shrink *proportionally*.
  // The chip rows hold the least, so they lose their labels first: both rows
  // cut off mid-text. Sizing to content is not the default here; it has to be
  // asked for (D42).
  chipScroll: { flexGrow: 0, flexShrink: 0 },
  chipRow: {
    flexDirection: 'row',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingBottom: space.sm,
  },
  chip: {
    paddingHorizontal: space.md,
    paddingVertical: space.xs + 2,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
    backgroundColor: color.surface,
  },
  // Selected is the text going dark, not the chip filling with colour. On a
  // screen whose whole point is that nothing is urgent, an accent-filled chip
  // is the loudest thing in view and reads as a warning.
  chipOn: { borderColor: color.text, backgroundColor: color.bg },
  chipPressed: { opacity: 0.6 },
  chipText: { fontSize: 13, color: color.muted },
  chipTextOn: { color: color.text, fontWeight: '600' },
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
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.sm },
  sectionHeader: {
    fontSize: 11,
    fontWeight: '700',
    letterSpacing: 0.8,
    textTransform: 'uppercase',
    color: color.faint,
    backgroundColor: color.bg,
    paddingTop: space.md,
    paddingBottom: space.sm,
  },
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
  rowBody: { flex: 1, gap: space.xs },
  rowText: { fontSize: 16, lineHeight: 22, color: color.text },
  meta: { flexDirection: 'row', alignItems: 'center', gap: space.xs + 2 },
  // One grey for the state and the date alike: a shelved item is not a worse
  // thing than a done one, and colouring them differently would say it was.
  metaText: { fontSize: 13, color: color.muted },
  metaDot: { fontSize: 13, color: color.faint },
  ghost: {
    paddingHorizontal: space.sm + 2,
    paddingVertical: space.xs + 2,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
    minWidth: 84,
    alignItems: 'center',
  },
  ghostText: { fontSize: 13, fontWeight: '600', color: color.accent },
  dimmed: { opacity: 0.5 },
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
