/**
 * `Today` (UC32) — what is due, what is coming, and tap to finish (UC16).
 *
 * Two blocks, one screen (D56). The top one is due and overdue: bounded by the
 * server, not filtered here, because the list has to be finishable and the way
 * it stops being finishable is by quietly widening. Below it, under a header,
 * sits everything active whose time is still ahead — which before this was on
 * no screen in the app at all.
 *
 * The separation is doing real work, not decoration. The count in the header,
 * the empty state and the phrase "Today is finished" all key off the top block
 * alone, so a screen with nine things next month on it still says the day is
 * done when the day is done. `Later` is a preview you read; only the block
 * above it is work you are being asked to clear.
 */
import { useCallback, useMemo, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  SectionList,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { router, useFocusEffect } from 'expo-router';

import { ApiError, markDone, today } from '../../lib/api';
import type { TodayItem } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { usePlayback } from '../../lib/playback';
import { dueLabel } from '../../lib/time';
import { showHeaders, todaySections } from '../../lib/today';
import { color, radius, space } from '../../lib/theme';

type RowProps = {
  item: TodayItem;
  onDone: (id: string) => void;
  onOpen: (id: string) => void;
  onPlay: (id: string) => void;
  playing: boolean;
  loadingAudio: boolean;
};

function Row({ item, onDone, onOpen, onPlay, playing, loadingAudio }: RowProps) {
  // Three targets, each with its own affordance. Finishing an item used to be
  // a tap anywhere on the row; it is now the circle, because the title had to
  // become the way into the detail screen (UC38, UC39) and a row that both
  // finishes and navigates cannot be either reliably.
  return (
    <View style={styles.row}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Mark done: ${item.text}`}
        hitSlop={12}
        onPress={() => onDone(item.id)}
        style={({ pressed }) => [styles.check, pressed && styles.checkPressed]}
      />

      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Open: ${item.text}`}
        onPress={() => onOpen(item.id)}
        style={({ pressed }) => [styles.rowBody, pressed && styles.rowPressed]}
      >
        <Text style={styles.rowText}>{item.text}</Text>
        <View style={styles.meta}>
          <Text style={[styles.due, item.overdue && styles.dueOverdue]}>
            {dueLabel(item.due_at)}
          </Text>
          {item.critical ? <Text style={styles.critical}>Critical</Text> : null}
          {item.parse_status !== 'ok' ? (
            <Text style={styles.flagged}>Check this</Text>
          ) : null}
        </View>
      </Pressable>

      {item.has_audio ? (
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
            <Text style={styles.playGlyph}>{playing ? '\u25a0' : '\u25b6'}</Text>
          )}
        </Pressable>
      ) : null}
    </View>
  );
}

export default function Today() {
  const { signOut } = useAuth();
  const playback = usePlayback();
  const [items, setItems] = useState<TodayItem[]>([]);
  const [later, setLater] = useState<TodayItem[]>([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      if (mode === 'refresh') setRefreshing(true);
      setError(null);
      try {
        const data = await today();
        setItems(data.items);
        // `?? []` and not a hard read: a phone running against a server that
        // predates the split should show the due block rather than crash.
        setLater(data.later ?? []);
        setTruncated(data.later_truncated ?? false);
      } catch (e) {
        if (e instanceof ApiError && e.isAuthError) {
          await signOut();
          return;
        }
        setError(e instanceof ApiError ? e.message : 'Could not load Today.');
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [signOut],
  );

  // Refetch on focus: a capture made on the other tab may have landed here.
  useFocusEffect(
    useCallback(() => {
      void load('initial');
    }, [load]),
  );

  const done = useCallback(
    async (id: string) => {
      const previous = { items, later };
      // Optimistic: the tap is the whole interaction, so it should not wait on
      // a round trip. A failure puts the row back and says so.
      //
      // Both blocks, because the circle is on every row: finishing something
      // early is a legitimate thing to do to a `Later` item and refusing it
      // would be admin work in the one place this app promises none (UC16).
      const without = (current: TodayItem[]) => current.filter((i) => i.id !== id);
      setItems(without);
      setLater(without);
      if (playback.activeId === id) playback.stop();
      setError(null);
      try {
        await markDone(id);
      } catch (e) {
        if (e instanceof ApiError && e.isAuthError) {
          await signOut();
          return;
        }
        setItems(previous.items);
        setLater(previous.later);
        setError(
          e instanceof ApiError ? `${e.message} Not marked done.` : 'Not marked done.',
        );
      }
    },
    [items, later, signOut, playback],
  );

  const sections = useMemo(() => todaySections(items, later), [items, later]);
  const headers = showHeaders(sections);

  // Said out loud even though the list below is not empty. The day being
  // finished is the thing this screen exists to be able to tell you, and a
  // `Later` block filling the space would otherwise silently swallow it.
  const cleared = items.length === 0 && later.length > 0;

  if (loading) {
    return (
      <SafeAreaView style={styles.centered} edges={['top', 'left', 'right']}>
        <ActivityIndicator color={color.muted} />
      </SafeAreaView>
    );
  }

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right']}>
      <View style={styles.header}>
        <Text style={styles.title}>Today</Text>
        {items.length > 0 ? (
          <Text style={styles.count}>{items.length}</Text>
        ) : null}
      </View>

      {error ?? playback.error ? (
        <Text style={styles.error}>{error ?? playback.error}</Text>
      ) : null}

      <SectionList
        sections={sections}
        keyExtractor={(item) => item.id}
        stickySectionHeadersEnabled={false}
        renderItem={({ item }) => (
          <Row
            item={item}
            onDone={(id) => void done(id)}
            onOpen={(id) => router.push(`/item/${id}`)}
            onPlay={(id) => void playback.toggle(id)}
            playing={playback.activeId === item.id && !playback.loading}
            loadingAudio={playback.activeId === item.id && playback.loading}
          />
        )}
        renderSectionHeader={({ section }) =>
          headers ? (
            <View style={styles.sectionHeader}>
              <Text style={styles.sectionTitle}>{section.title}</Text>
              <Text style={styles.sectionCount}>{section.data.length}</Text>
            </View>
          ) : null
        }
        ListHeaderComponent={
          cleared ? (
            <View style={styles.cleared}>
              <Text style={styles.clearedTitle}>Nothing due.</Text>
              <Text style={styles.clearedBody}>Today is finished.</Text>
            </View>
          ) : null
        }
        ListFooterComponent={
          truncated ? (
            <Text style={styles.footnote}>
              More further out than fits here — search the shelf for it.
            </Text>
          ) : null
        }
        contentContainerStyle={sections.length === 0 ? styles.emptyWrap : styles.list}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => void load('refresh')}
            tintColor={color.muted}
          />
        }
        ListEmptyComponent={
          <View style={styles.empty}>
            <Text style={styles.emptyTitle}>Nothing due.</Text>
            <Text style={styles.emptyBody}>
              Today is finished. Anything without a time is on the shelf.
            </Text>
          </View>
        }
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
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: space.sm,
    paddingHorizontal: space.lg,
    paddingTop: space.sm,
    paddingBottom: space.md,
  },
  title: { fontSize: 28, fontWeight: '700', color: color.text, letterSpacing: -0.6 },
  count: { fontSize: 16, fontWeight: '600', color: color.faint },
  error: {
    marginHorizontal: space.lg,
    marginBottom: space.sm,
    fontSize: 14,
    lineHeight: 20,
    color: color.danger,
  },
  list: { paddingHorizontal: space.lg, paddingBottom: space.xl, gap: space.sm },
  sectionHeader: {
    flexDirection: 'row',
    alignItems: 'baseline',
    gap: space.sm,
    paddingTop: space.md,
  },
  sectionTitle: {
    fontSize: 13,
    fontWeight: '700',
    letterSpacing: 0.6,
    color: color.faint,
    textTransform: 'uppercase',
  },
  sectionCount: { fontSize: 13, color: color.faint },
  cleared: { paddingTop: space.sm, gap: space.xs },
  clearedTitle: { fontSize: 18, fontWeight: '600', color: color.text },
  clearedBody: { fontSize: 15, lineHeight: 22, color: color.muted },
  footnote: {
    paddingTop: space.md,
    fontSize: 13,
    lineHeight: 19,
    color: color.faint,
  },
  flagged: { fontSize: 12, fontWeight: '600', color: color.muted },
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
  row: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: space.md,
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    padding: space.md,
  },
  rowPressed: { opacity: 0.6 },
  checkPressed: { backgroundColor: color.border },
  check: {
    width: 22,
    height: 22,
    borderRadius: radius.pill,
    borderWidth: 2,
    borderColor: color.border,
    marginTop: 2,
  },
  rowBody: { flex: 1, gap: space.xs },
  rowText: { fontSize: 16, lineHeight: 22, color: color.text },
  meta: { flexDirection: 'row', alignItems: 'center', gap: space.sm },
  due: { fontSize: 13, color: color.muted },
  dueOverdue: { color: color.overdue, fontWeight: '600' },
  critical: {
    fontSize: 11,
    fontWeight: '700',
    letterSpacing: 0.5,
    color: color.overdue,
    textTransform: 'uppercase',
  },
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
