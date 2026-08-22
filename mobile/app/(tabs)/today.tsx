/**
 * `Today` (UC32) — due and overdue only, and tap to finish (UC16).
 *
 * The bound is the server's, not a filter here: the list has to be finishable,
 * and the way it stops being finishable is by quietly widening.
 */
import { useCallback, useState } from 'react';
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
import { useFocusEffect } from 'expo-router';

import { ApiError, markDone, today } from '../../lib/api';
import type { TodayItem } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { dueLabel } from '../../lib/time';
import { color, radius, space } from '../../lib/theme';

function Row({ item, onDone }: { item: TodayItem; onDone: (id: string) => void }) {
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Mark done: ${item.text}`}
      onPress={() => onDone(item.id)}
      style={({ pressed }) => [styles.row, pressed && styles.rowPressed]}
    >
      <View style={styles.check} />
      <View style={styles.rowBody}>
        <Text style={styles.rowText}>{item.text}</Text>
        <View style={styles.meta}>
          <Text style={[styles.due, item.overdue && styles.dueOverdue]}>
            {dueLabel(item.due_at)}
          </Text>
          {item.critical ? <Text style={styles.critical}>Critical</Text> : null}
        </View>
      </View>
    </Pressable>
  );
}

export default function Today() {
  const { signOut } = useAuth();
  const [items, setItems] = useState<TodayItem[]>([]);
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
      const previous = items;
      // Optimistic: the tap is the whole interaction, so it should not wait on
      // a round trip. A failure puts the row back and says so.
      setItems((current) => current.filter((i) => i.id !== id));
      setError(null);
      try {
        await markDone(id);
      } catch (e) {
        if (e instanceof ApiError && e.isAuthError) {
          await signOut();
          return;
        }
        setItems(previous);
        setError(
          e instanceof ApiError ? `${e.message} Not marked done.` : 'Not marked done.',
        );
      }
    },
    [items, signOut],
  );

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

      {error ? <Text style={styles.error}>{error}</Text> : null}

      <FlatList
        data={items}
        keyExtractor={(item) => item.id}
        renderItem={({ item }) => <Row item={item} onDone={(id) => void done(id)} />}
        contentContainerStyle={items.length === 0 ? styles.emptyWrap : styles.list}
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
