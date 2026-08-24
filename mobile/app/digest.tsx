/**
 * The weekly digest (UC31) — what the system did while you were not looking.
 *
 * **This is the load-bearing screen of the whole decay bet.** UC22 was dropped,
 * so items shelve and drop in silence; nothing announces a transition as it
 * happens, and the Shelf deliberately refuses to flag them either. If this
 * screen does not exist, "the system reads your silence as an answer" is
 * indistinguishable from "the app loses things".
 *
 * Two sections in two different tenses, and the difference is the design:
 *
 * - **What moved** is history. It already happened, there is nothing to do
 *   about it here, and the rows are informational — you open one if you
 *   disagree with the decision. Reactivating from here would turn the week's
 *   account into a to-do list.
 * - **About to drop** is a forecast, and it is the half with something to act
 *   on: everything on it is still recoverable, and only until it is not. So
 *   this is the section that gets the one action on the screen.
 *
 * The order is deliberate too. The forecast comes *first*, because it is the
 * part that is still actionable; putting the history above it would bury the
 * only thing you can do anything about under a list of things you cannot.
 *
 * Not a tab. Four is the ceiling (D44), and this is a place you are sent to by
 * a notification once a week, not one you live in.
 */
import { useCallback, useState } from 'react';
import {
  ActivityIndicator,
  Pressable,
  RefreshControl,
  ScrollView,
  StyleSheet,
  Text,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { router, useFocusEffect } from 'expo-router';

import { ApiError, digest, reactivateItem } from '../lib/api';
import type { DecayedItem, DigestResponse, ExpiringItem } from '../lib/api';
import { useAuth } from '../lib/auth';
import { capturedOnLabel, dropsInLabel, weekLabel } from '../lib/time';
import { color, radius, space } from '../lib/theme';

/** What a row says about where the item ended up, when that is not obvious. */
const MOVED_ON: Record<string, string> = {
  active: 'You brought it back',
  done: 'You finished it',
};

type SectionProps = {
  title: string;
  subtitle: string;
  shown: number;
  total: number;
  children: React.ReactNode;
};

function Section({ title, subtitle, shown, total, children }: SectionProps) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      <Text style={styles.sectionSubtitle}>{subtitle}</Text>
      {children}
      {total > shown ? (
        <Text style={styles.more}>
          and {total - shown} more — the Shelf has all of them
        </Text>
      ) : null}
    </View>
  );
}

function MovedRow({ item }: { item: DecayedItem }) {
  const since = MOVED_ON[item.state_now];
  return (
    <Pressable
      accessibilityRole="button"
      accessibilityLabel={`Open: ${item.text}`}
      onPress={() => router.push(`/item/${item.id}`)}
      style={({ pressed }) => [styles.movedRow, pressed && styles.rowPressed]}
    >
      <Text style={styles.rowText} numberOfLines={3}>
        {item.text}
      </Text>
      <View style={styles.meta}>
        <Text style={styles.metaText}>{capturedOnLabel(item.at)}</Text>
        {since ? (
          <>
            <Text style={styles.metaDot}>·</Text>
            {/* Not a correction of the digest — the transition still happened
                this week. It is the second half of the same fact, and it is
                the evidence the decay constants are eventually tuned from. */}
            <Text style={styles.metaText}>{since}</Text>
          </>
        ) : null}
      </View>
    </Pressable>
  );
}

type ExpiringRowProps = {
  item: ExpiringItem;
  onKeep: (id: string) => void;
  busy: boolean;
};

function ExpiringRow({ item, onKeep, busy }: ExpiringRowProps) {
  return (
    <View style={styles.row}>
      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Open: ${item.text}`}
        onPress={() => router.push(`/item/${item.id}`)}
        style={({ pressed }) => [styles.rowBody, pressed && styles.rowPressed]}
      >
        <Text style={styles.rowText} numberOfLines={3}>
          {item.text}
        </Text>
        <View style={styles.meta}>
          <Text style={styles.metaText}>{dropsInLabel(item.drops_at)}</Text>
          <Text style={styles.metaDot}>·</Text>
          <Text style={styles.metaText}>
            untouched since {capturedOnLabel(item.untouched_since)}
          </Text>
        </View>
      </Pressable>

      <Pressable
        accessibilityRole="button"
        accessibilityLabel={`Keep: ${item.text}`}
        disabled={busy}
        hitSlop={8}
        onPress={() => onKeep(item.id)}
        style={({ pressed }) => [
          styles.ghost,
          pressed && styles.ghostPressed,
          busy && styles.dimmed,
        ]}
      >
        {busy ? (
          <ActivityIndicator color={color.muted} size="small" />
        ) : (
          <Text style={styles.ghostText}>Keep</Text>
        )}
      </Pressable>
    </View>
  );
}

export default function Digest() {
  const { signOut } = useAuth();

  const [week, setWeek] = useState<DigestResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [keeping, setKeeping] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

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
      if (mode === 'refresh') setRefreshing(true);
      else setLoading(true);
      setError(null);
      try {
        setWeek(await digest());
      } catch (e) {
        await failed(e, 'Could not load the week.');
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [failed],
  );

  // On focus rather than once: the "about to drop" half is a forecast off the
  // current shelf, so coming back from an item you just edited has to show
  // that the item is no longer on its way out.
  useFocusEffect(
    useCallback(() => {
      void load('initial');
    }, [load]),
  );

  /**
   * Take something back off the drop path (UC20).
   *
   * The row leaves this list immediately rather than waiting for a refetch:
   * the whole point of the section is that the list shrinks as you deal with
   * it, and a row that stays put after you have acted reads as a failure.
   */
  const keep = useCallback(
    async (id: string) => {
      setKeeping(id);
      try {
        await reactivateItem(id);
        // Not published to `lib/itemEvents`: the Shelf does not announce its
        // own reactivations either, and it refetches when the filters change.
        // The one screen that has to be right immediately is this one.
        setWeek((current) =>
          current
            ? {
                ...current,
                expiring: current.expiring.filter((row) => row.id !== id),
                expiring_total: Math.max(0, current.expiring_total - 1),
              }
            : current,
        );
      } catch (e) {
        await failed(e, 'That did not go through.');
      } finally {
        setKeeping(null);
      }
    },
    [failed],
  );

  if (loading && !week) {
    return (
      <SafeAreaView style={styles.screen} edges={['top', 'bottom']}>
        <View style={styles.centre}>
          <ActivityIndicator color={color.muted} />
        </View>
      </SafeAreaView>
    );
  }

  const nothing =
    week && !week.shelved_total && !week.dropped_total && !week.expiring_total;

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'bottom']}>
      <View style={styles.header}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Back"
          hitSlop={12}
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/today'))}
        >
          <Text style={styles.back}>Back</Text>
        </Pressable>
        <Text style={styles.title}>Your week</Text>
        <Text style={styles.week}>
          {week ? weekLabel(week.period_start, week.period_end) : ''}
        </Text>
      </View>

      <ScrollView
        contentContainerStyle={styles.body}
        refreshControl={
          <RefreshControl
            refreshing={refreshing}
            onRefresh={() => void load('refresh')}
            tintColor={color.muted}
          />
        }
      >
        {error ? <Text style={styles.error}>{error}</Text> : null}

        {nothing ? (
          <Text style={styles.empty}>
            Nothing moved by itself, and nothing is near dropping.
          </Text>
        ) : null}

        {week && week.expiring.length ? (
          <Section
            title="About to drop"
            subtitle={`Shelved and untouched. These go in the next ${week.warn_days} days unless you keep them.`}
            shown={week.expiring.length}
            total={week.expiring_total}
          >
            {week.expiring.map((item) => (
              <ExpiringRow
                key={item.id}
                item={item}
                onKeep={(id) => void keep(id)}
                busy={keeping === item.id}
              />
            ))}
          </Section>
        ) : null}

        {week && week.shelved.length ? (
          <Section
            title="Shelved"
            subtitle="You did not answer these enough times, so they came off Today."
            shown={week.shelved.length}
            total={week.shelved_total}
          >
            {week.shelved.map((item) => (
              <MovedRow key={`${item.id}-${item.at}`} item={item} />
            ))}
          </Section>
        ) : null}

        {week && week.dropped.length ? (
          <Section
            title="Dropped"
            subtitle="Long enough on the shelf, untouched. They are still here if you search."
            shown={week.dropped.length}
            total={week.dropped_total}
          >
            {week.dropped.map((item) => (
              <MovedRow key={`${item.id}-${item.at}`} item={item} />
            ))}
          </Section>
        ) : null}
      </ScrollView>
    </SafeAreaView>
  );
}

export { RouteError as ErrorBoundary } from '../lib/RouteError';

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  centre: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  header: {
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    paddingBottom: space.md,
    gap: space.xs,
  },
  back: { color: color.muted, fontSize: 15 },
  title: { color: color.text, fontSize: 28, fontWeight: '600' },
  week: { color: color.faint, fontSize: 14 },
  body: { paddingHorizontal: space.md, paddingBottom: space.xl, gap: space.lg },
  section: { gap: space.sm },
  sectionTitle: { color: color.text, fontSize: 17, fontWeight: '600' },
  sectionSubtitle: { color: color.muted, fontSize: 13, lineHeight: 18 },
  movedRow: {
    gap: space.xs,
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  row: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: space.sm,
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    paddingHorizontal: space.md,
    paddingVertical: space.sm,
  },
  rowBody: { flex: 1, gap: space.xs },
  rowPressed: { opacity: 0.6 },
  rowText: { color: color.text, fontSize: 16, lineHeight: 22 },
  meta: { flexDirection: 'row', alignItems: 'center', gap: space.xs, flexWrap: 'wrap' },
  metaText: { color: color.faint, fontSize: 13 },
  metaDot: { color: color.faint, fontSize: 13 },
  more: { color: color.faint, fontSize: 13, paddingTop: space.xs },
  ghost: {
    paddingHorizontal: space.sm,
    paddingVertical: space.xs,
    borderRadius: radius.pill,
    borderWidth: 1,
    borderColor: color.border,
  },
  ghostPressed: { opacity: 0.6 },
  ghostText: { color: color.muted, fontSize: 14 },
  dimmed: { opacity: 0.5 },
  empty: { color: color.muted, fontSize: 15, lineHeight: 22 },
  error: { color: color.danger, fontSize: 14 },
});
