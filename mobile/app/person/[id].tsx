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
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Stack, router, useLocalSearchParams } from 'expo-router';

import { ApiError, person as fetchPerson } from '../../lib/api';
import type { ItemState, Person, PersonItem } from '../../lib/api';
import { useAuth } from '../../lib/auth';
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
};

function Row({ item, onOpen, onPlay, playing, loadingAudio }: RowProps) {
  return (
    <View style={styles.row}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Open: ${item.text}`}
        onPress={() => onOpen(item.id)}
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
          accessibilityLabel="Back to people"
          hitSlop={12}
          onPress={() => router.back()}
        >
          <Text style={styles.back}>‹ People</Text>
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
      </View>

      {error ?? playback.error ? (
        <Text style={styles.error}>{error ?? playback.error}</Text>
      ) : null}

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
