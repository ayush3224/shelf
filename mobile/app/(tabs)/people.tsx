/**
 * People (UC47) — browse and search everyone who has been mentioned.
 *
 * This is a second index over the same items, not a subset of the Shelf. You
 * come to the Shelf when you remember roughly *when* you said something, and
 * here when you remember *who* it was about, and neither list contains the
 * other — a person-note that is due today appears here and never on the Shelf,
 * which excludes `active` by definition. That is why it earns a tab rather than
 * sitting inside one.
 *
 * **Recall is manual and stays that way.** Nothing on this screen surfaces
 * itself: there is no "you are seeing Ravi in an hour" and no calendar
 * triggering. That is deferred deliberately — it needs UC43 and a delivery
 * tier, and it should not be built until the version you have to go and open
 * has been used enough to know what is worth pushing.
 *
 * Ordered by who was mentioned most recently rather than alphabetically. The
 * list exists to find somebody, and the person you spoke about this morning is
 * a likelier target than the one from last year; the name is only the tiebreak.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  Pressable,
  RefreshControl,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { router, useFocusEffect } from 'expo-router';

import { ApiError, people as fetchPeople } from '../../lib/api';
import type { Person } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { capturedOnLabel } from '../../lib/time';
import { color, radius, space } from '../../lib/theme';

/** Long enough that typing a name is one request, short enough to feel live. */
const SEARCH_DEBOUNCE_MS = 300;

/** How the aliases read under a name, when there are any worth showing. */
function alsoKnownAs(person: Person): string | null {
  if (!person.aliases.length) return null;
  return `also ${person.aliases.join(', ')}`;
}

function Row({ person, onOpen }: { person: Person; onOpen: (id: string) => void }) {
  const aka = alsoKnownAs(person);

  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Open ${person.name}`}
      onPress={() => onOpen(person.id)}
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
    >
      <View style={styles.rowBody}>
        <Text style={styles.name}>{person.name}</Text>
        {aka ? <Text style={styles.aka}>{aka}</Text> : null}
      </View>
      <View style={styles.rowMeta}>
        <Text style={styles.count}>{person.mentions}</Text>
        {person.last_mentioned ? (
          <Text style={styles.when}>{capturedOnLabel(person.last_mentioned)}</Text>
        ) : null}
      </View>
    </Pressable>
  );
}

export default function People() {
  const { signOut } = useAuth();

  const [term, setTerm] = useState('');
  const [search, setSearch] = useState('');
  const [rows, setRows] = useState<Person[]>([]);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /** Which query an answer belongs to; a stale one is dropped, not rendered. */
  const generation = useRef(0);

  useEffect(() => {
    const next = term.trim();
    if (next === search) return;
    const timer = setTimeout(() => setSearch(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [term, search]);

  const load = useCallback(
    async (mode: 'initial' | 'refresh') => {
      const mine = ++generation.current;
      if (mode === 'refresh') setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        const data = await fetchPeople(search || undefined);
        if (generation.current !== mine) return;
        setRows(data.people);
      } catch (e) {
        if (generation.current !== mine) return;
        if (e instanceof ApiError && e.isAuthError) {
          await signOut();
          return;
        }
        setError(e instanceof ApiError ? e.message : 'Could not load people.');
      } finally {
        if (generation.current === mine) {
          setLoading(false);
          setRefreshing(false);
        }
      }
    },
    [search, signOut],
  );

  useEffect(() => {
    void load('initial');
  }, [load]);

  // A capture made on another tab may have named somebody new, and this list
  // is short enough that refetching it costs nothing worth protecting.
  useFocusEffect(
    useCallback(() => {
      void load('refresh');
    }, [load]),
  );

  const searching = search.length > 0;
  const empty = useMemo(
    () => ({
      title: searching ? 'Nobody by that name.' : 'No one yet.',
      body: searching
        ? 'Names come from what you say — try the other half of it.'
        : 'Say a name in a capture and whoever you mentioned turns up here.',
    }),
    [searching],
  );

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right']}>
      <View style={styles.header}>
        <Text style={styles.title}>People</Text>
        {searching && !loading ? (
          <Text style={styles.headerCount}>{rows.length}</Text>
        ) : null}
      </View>

      <TextInput
        style={styles.search}
        value={term}
        onChangeText={setTerm}
        placeholder="Search people"
        placeholderTextColor={color.faint}
        accessibilityLabel="Search people"
        autoCapitalize="words"
        autoCorrect={false}
        returnKeyType="search"
        clearButtonMode="while-editing"
      />

      {error ? <Text style={styles.error}>{error}</Text> : null}

      {loading ? (
        <View style={styles.centered}>
          <ActivityIndicator color={color.muted} />
        </View>
      ) : (
        <FlatList
          data={rows}
          keyExtractor={(person) => person.id}
          keyboardShouldPersistTaps="handled"
          keyboardDismissMode="on-drag"
          renderItem={({ item }) => (
            <Row person={item} onOpen={(id) => router.push(`/person/${id}`)} />
          )}
          contentContainerStyle={rows.length === 0 ? styles.emptyWrap : styles.list}
          refreshControl={
            <RefreshControl
              refreshing={refreshing}
              onRefresh={() => void load('refresh')}
              tintColor={color.muted}
            />
          }
          ListEmptyComponent={
            <View style={styles.empty}>
              <Text style={styles.emptyTitle}>{empty.title}</Text>
              <Text style={styles.emptyBody}>{empty.body}</Text>
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
  headerCount: { fontSize: 16, fontWeight: '600', color: color.faint },
  search: {
    marginHorizontal: space.lg,
    marginBottom: space.md,
    paddingHorizontal: space.md,
    paddingVertical: space.sm + 2,
    fontSize: 16,
    color: color.text,
    backgroundColor: color.surface,
    borderWidth: 1,
    borderColor: color.border,
    borderRadius: radius.md,
  },
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
  rowBody: { flex: 1, gap: 2 },
  name: { fontSize: 17, color: color.text, fontWeight: '600' },
  aka: { fontSize: 13, color: color.faint },
  rowMeta: { alignItems: 'flex-end', gap: 2 },
  count: { fontSize: 15, fontWeight: '600', color: color.muted },
  when: { fontSize: 12, color: color.faint },
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
