/**
 * The weekly digest (UC31) — what the system did while you were not looking.
 *
 * **This is the load-bearing screen of the whole decay bet.** UC22 was dropped,
 * so items shelve and drop in silence; nothing announces a transition as it
 * happens, and the Shelf deliberately refuses to flag them either. If this
 * screen does not exist, "the system reads your silence as an answer" is
 * indistinguishable from "the app loses things".
 *
 * The screen is organised by **what you can do about it**, not by what
 * happened, and the two do not line up:
 *
 * - **Still open** — about to drop, and shelved this week. Every row carries a
 *   decision, and together they are exactly the review deck (UC30). This half
 *   is offered as a deck first and a list second.
 * - **Closed this week** — completed, and dropped. Terminal: there is nothing
 *   to swipe, so these are never cards. They are collapsed behind their counts
 *   and expand on a tap, because a count is the whole answer most weeks and
 *   the list is what you want on the weeks it is not.
 *
 * Ordering follows the same rule. The forecast — the only part still
 * recoverable, and only until it is not — sits above the history. Putting the
 * account of decisions already made above the list you can still act on would
 * bury the point of the screen under its own preamble.
 *
 * Not a tab. Four is the ceiling (D44), and this is a place you are sent to by
 * a notification once a week, not one you live in.
 */
import { useCallback, useMemo, useState } from 'react';
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
import type { DigestResponse, ExpiringItem, MovedItem } from '../lib/api';
import { useAuth } from '../lib/auth';
import { buildDeck } from '../lib/review';
import {
  capturedOnLabel,
  dropsInLabel,
  dueAgeLabel,
  shelvedAgeLabel,
  untouchedLabel,
  weekLabel,
} from '../lib/time';
import { color, radius, space } from '../lib/theme';

/** What a row says about where the item ended up, when that is not obvious. */
const MOVED_ON: Record<string, string> = {
  active: 'You brought it back',
  done: 'You finished it',
};

type SectionProps = {
  title: string;
  subtitle?: string;
  shown: number;
  total: number;
  children: React.ReactNode;
};

function Section({ title, subtitle, shown, total, children }: SectionProps) {
  return (
    <View style={styles.section}>
      <Text style={styles.sectionTitle}>{title}</Text>
      {subtitle ? <Text style={styles.sectionSubtitle}>{subtitle}</Text> : null}
      {children}
      {total > shown ? (
        <Text style={styles.more}>
          and {total - shown} more — the Shelf has all of them
        </Text>
      ) : null}
    </View>
  );
}

/**
 * A closed-out section: a count you can open (UC31).
 *
 * Collapsed by default because on most weeks the number *is* the answer —
 * "4 done, nothing dropped" is a complete report — and an expanded list of
 * four things you already know you did would push the half of the screen that
 * still needs a decision off the bottom.
 */
function Summary({
  title,
  total,
  items,
  empty,
}: {
  title: string;
  total: number;
  items: MovedItem[];
  empty: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <View style={styles.summary}>
      <Pressable
        accessibilityRole="button"
        accessibilityState={{ expanded: open, disabled: total === 0 }}
        accessibilityLabel={`${title}: ${total}`}
        disabled={total === 0}
        onPress={() => setOpen((v) => !v)}
        style={({ pressed }) => [styles.summaryHead, pressed && styles.rowPressed]}
      >
        <Text style={styles.summaryCount}>{total}</Text>
        <Text style={styles.summaryTitle}>{title}</Text>
        {total > 0 ? <Text style={styles.chevron}>{open ? '−' : '+'}</Text> : null}
      </Pressable>

      {total === 0 ? <Text style={styles.summaryEmpty}>{empty}</Text> : null}

      {open ? (
        <View style={styles.summaryBody}>
          {items.map((item) => (
            <Pressable
              key={`${item.id}-${item.at}`}
              accessibilityRole="button"
              accessibilityLabel={`Open: ${item.text}`}
              onPress={() => router.push(`/item/${item.id}`)}
              style={({ pressed }) => [styles.summaryRow, pressed && styles.rowPressed]}
            >
              <Text style={styles.summaryRowText} numberOfLines={2}>
                {item.text}
              </Text>
              <Text style={styles.metaText}>{capturedOnLabel(item.at)}</Text>
            </Pressable>
          ))}
          {total > items.length ? (
            <Text style={styles.more}>and {total - items.length} more</Text>
          ) : null}
        </View>
      ) : null}
    </View>
  );
}

function ShelvedRow({ item }: { item: MovedItem }) {
  const since = MOVED_ON[item.state_now];
  const due = dueAgeLabel(item.due_at);
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
        <Text style={styles.metaText}>{shelvedAgeLabel(item.at)}</Text>
        {due ? (
          <>
            <Text style={styles.metaDot}>·</Text>
            <Text style={styles.metaText}>{due.toLowerCase()}</Text>
          </>
        ) : null}
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
  const due = dueAgeLabel(item.due_at);
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
          <Text style={styles.metaText}>{untouchedLabel(item.untouched_since)}</Text>
          {due ? (
            <>
              <Text style={styles.metaDot}>·</Text>
              <Text style={styles.metaText}>{due.toLowerCase()}</Text>
            </>
          ) : null}
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
  // current shelf, so coming back from the deck — or from an item you just
  // edited — has to show what is no longer on its way out.
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

  const deck = useMemo(() => (week ? buildDeck(week) : []), [week]);

  if (loading && !week) {
    return (
      <SafeAreaView style={styles.screen} edges={['top', 'bottom']}>
        <View style={styles.centre}>
          <ActivityIndicator color={color.muted} />
        </View>
      </SafeAreaView>
    );
  }

  const nothingOpen = !deck.length;
  const nothingClosed = week ? !week.done_total && !week.dropped_total : true;

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

        {nothingOpen && nothingClosed ? (
          <Text style={styles.empty}>
            Nothing moved by itself, nothing closed, and nothing is near dropping.
          </Text>
        ) : null}

        {/* The deck, offered before the lists it is made of. Everything below
            is readable on its own; this is the two-minute version of it. */}
        {deck.length ? (
          <Pressable
            accessibilityRole="button"
            accessibilityLabel={`Review ${deck.length} items`}
            onPress={() => router.push('/review')}
            style={({ pressed }) => [styles.review, pressed && styles.rowPressed]}
          >
            <Text style={styles.reviewText}>
              Review {deck.length} {deck.length === 1 ? 'item' : 'items'}
            </Text>
            <Text style={styles.reviewHint}>One at a time, four ways out</Text>
          </Pressable>
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
              <ShelvedRow key={`${item.id}-${item.at}`} item={item} />
            ))}
          </Section>
        ) : null}

        {week ? (
          <Section title="Closed this week" shown={0} total={0}>
            <Summary
              title="finished"
              total={week.done_total}
              items={week.done}
              empty="Nothing finished this week."
            />
            <Summary
              title="dropped off the shelf"
              total={week.dropped_total}
              items={week.dropped}
              empty="Nothing dropped this week."
            />
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
  review: {
    backgroundColor: color.accent,
    borderRadius: radius.md,
    paddingHorizontal: space.md,
    paddingVertical: space.md,
    gap: 2,
  },
  reviewText: { color: color.accentText, fontSize: 17, fontWeight: '600' },
  reviewHint: { color: color.accentText, fontSize: 13, opacity: 0.8 },
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
  summary: { gap: space.xs },
  summaryHead: { flexDirection: 'row', alignItems: 'baseline', gap: space.sm },
  summaryCount: { color: color.text, fontSize: 22, fontWeight: '600', minWidth: 26 },
  summaryTitle: { color: color.muted, fontSize: 15, flex: 1 },
  chevron: { color: color.faint, fontSize: 18, paddingHorizontal: space.xs },
  summaryEmpty: { color: color.faint, fontSize: 13, paddingLeft: 26 + space.sm },
  summaryBody: { gap: space.xs, paddingLeft: 26 + space.sm },
  summaryRow: {
    gap: 2,
    borderLeftWidth: 1,
    borderLeftColor: color.border,
    paddingLeft: space.sm,
    paddingVertical: space.xs,
  },
  summaryRowText: { color: color.text, fontSize: 15, lineHeight: 20 },
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
