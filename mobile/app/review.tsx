/**
 * The weekly review deck (UC30) — one card, four ways out.
 *
 * The doing half of Sunday. The digest (UC31) is the account of the week; this
 * is the two minutes in which you answer it.
 *
 * **The deck is bounded to what carries a decision** — what decayed this week
 * and what is about to drop — and that bound is the feature, not a shortcut.
 * `CLAUDE.md` is explicit that a feature requiring admin work to keep state
 * accurate is the wrong feature, and a deck of "everything shelved" would be
 * ninety days of accumulation: the same wall `Today` is bounded to avoid,
 * dealt one card at a time. What was completed and what already dropped are
 * terminal, so they stay on the digest as summary; there is nothing to swipe.
 *
 * **Every card says how old it is, twice**, and the two numbers rarely agree.
 * "Shelved 4 days ago" is how long the system has had it put away. "Due 9 days
 * ago" is how long you have been not doing it. Without the second, the swipe
 * is a guess — the shelving is always more recent than the neglect that caused
 * it, so age-since-shelving alone flatters every card in the deck.
 *
 * **Swiping is not the only way through.** The four directions are also four
 * buttons, and not as a fallback: a gesture-only screen is unusable with a
 * screen reader, and it is also the only screen in this app where the action
 * is invisible until you have already done it. The buttons are the legend.
 *
 * **Answers are optimistic.** The card leaves as soon as you have answered it
 * and the request goes out behind it, because a deck that pauses on each card
 * is not a two-minute review. Anything that fails to save is counted and named
 * at the end rather than interrupting the run — you find out, but after you
 * have finished rather than instead of finishing.
 */
import { useCallback, useMemo, useRef, useState } from 'react';
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';
import { Gesture, GestureDetector } from 'react-native-gesture-handler';
import Animated, {
  runOnJS,
  useAnimatedStyle,
  useSharedValue,
  withSpring,
  withTiming,
} from 'react-native-reanimated';
import { router, useFocusEffect } from 'expo-router';

import { ApiError, digest, markDone, reactivateItem, setItemState } from '../lib/api';
import { useAuth } from '../lib/auth';
import { DECISIONS, buildDeck, directionOf } from '../lib/review';
import type { Direction, ReviewCard } from '../lib/review';
import { dropsInLabel, dueAgeLabel, shelvedAgeLabel, untouchedLabel } from '../lib/time';
import { color, radius, space } from '../lib/theme';

/** How far a drag has to travel before it is an answer rather than a fidget. */
const THRESHOLD = 90;

/** How far off-screen a committed card flies. Well past any phone's width. */
const EXIT = 700;

/** The order the buttons are laid out in, which is also the legend's order. */
const BUTTONS: Direction[] = ['left', 'up', 'right', 'down'];

/** What each direction is called, spelled out where the gesture cannot be. */
const HINT: Record<Direction, string> = {
  left: 'Leave it on the shelf',
  up: 'Mark it done',
  right: 'Bring it back to Today',
  down: 'Drop it for good',
};

/** The arrow drawn on each button, so the mapping is learnable in one pass. */
const ARROW: Record<Direction, string> = {
  left: '←',
  up: '↑',
  right: '→',
  down: '↓',
};

function Ages({ card }: { card: ReviewCard }) {
  const due = dueAgeLabel(card.dueAt);
  return (
    <View style={styles.ages}>
      {/* The deadline first on an expiring card: it is the reason the card is
          in the deck at all, and the only number here that is about the
          future rather than the past. */}
      {card.dropsAt ? <Text style={styles.urgent}>{dropsInLabel(card.dropsAt)}</Text> : null}
      {card.shelvedAt ? (
        <Text style={styles.age}>{shelvedAgeLabel(card.shelvedAt)}</Text>
      ) : null}
      {card.untouchedSince ? (
        <Text style={styles.age}>{untouchedLabel(card.untouchedSince)}</Text>
      ) : null}
      {due ? (
        // The one that decides the swipe, and the one the other numbers
        // quietly understate.
        <Text style={styles.age}>{due}</Text>
      ) : null}
      {!due ? (
        <Text style={styles.age}>Captured without a time — never had a deadline</Text>
      ) : null}
    </View>
  );
}

export default function Review() {
  const { signOut } = useAuth();

  const [deck, setDeck] = useState<ReviewCard[]>([]);
  const [index, setIndex] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [kept, setKept] = useState(0);
  const [failures, setFailures] = useState<string[]>([]);

  const x = useSharedValue(0);
  const y = useSharedValue(0);

  /**
   * Answers still in flight.
   *
   * A ref rather than state: nothing renders it, and re-rendering the deck on
   * every request settling would animate the card that is already gone.
   */
  const pending = useRef(0);

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

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const week = await digest();
      setDeck(buildDeck(week));
      setIndex(0);
      setKept(0);
      setFailures([]);
    } catch (e) {
      await failed(e, 'Could not load the review.');
    } finally {
      setLoading(false);
    }
  }, [failed]);

  // Loaded once per visit rather than on every focus: the deck is a run you
  // are partway through, and refetching under it would deal cards you have
  // already answered.
  useFocusEffect(
    useCallback(() => {
      void load();
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, []),
  );

  /**
   * Send the answer, behind the card that has already gone.
   *
   * `left` writes nothing at all: every card in this deck is already shelved,
   * so "leave it" is the state the item is in. Touching the row to record the
   * non-decision would restart its ninety-day drop clock (D37) and silently
   * turn "I looked at this and did nothing" into "keep it another three
   * months" — the opposite of what the gesture said.
   */
  const commit = useCallback(async (card: ReviewCard, direction: Direction) => {
    const decision = DECISIONS[direction];
    if (!decision.writes) return;

    pending.current += 1;
    try {
      if (decision.state === 'done') await markDone(card.id);
      else if (decision.state === 'active') await reactivateItem(card.id);
      else await setItemState(card.id, decision.state);
      if (decision.state === 'active') setKept((n) => n + 1);
    } catch (e) {
      console.error(`[shelf/review] ${decision.label} failed for ${card.id}:`, e);
      setFailures((current) => [...current, card.text]);
    } finally {
      pending.current -= 1;
    }
  }, []);

  /** Answer the top card and deal the next one. */
  const answer = useCallback(
    (direction: Direction) => {
      const card = deck[index];
      if (!card) return;
      void commit(card, direction);
      x.value = 0;
      y.value = 0;
      setIndex((n) => n + 1);
    },
    [deck, index, commit, x, y],
  );

  /**
   * Fly the card out the way it was answered, then deal the next.
   *
   * The animation runs before the state change rather than after, so the card
   * that leaves is the one that was answered — advancing first would swap the
   * text under the departing card.
   */
  const fling = useCallback(
    (direction: Direction) => {
      const horizontal = direction === 'left' || direction === 'right';
      const to = direction === 'left' || direction === 'up' ? -EXIT : EXIT;
      const done = () => answer(direction);
      if (horizontal) {
        x.value = withTiming(to, { duration: 180 }, (finished) => {
          if (finished) runOnJS(done)();
        });
      } else {
        y.value = withTiming(to, { duration: 180 }, (finished) => {
          if (finished) runOnJS(done)();
        });
      }
    },
    [answer, x, y],
  );

  const pan = useMemo(
    () =>
      Gesture.Pan()
        .onUpdate((e) => {
          x.value = e.translationX;
          y.value = e.translationY;
        })
        .onEnd((e) => {
          const direction = directionOf(e.translationX, e.translationY, THRESHOLD);
          if (!direction) {
            // Not far enough to mean anything. Snap back rather than guessing:
            // three of the four answers are irreversible-ish, and a card that
            // resolves a half-hearted drag is a card you will fight.
            x.value = withSpring(0);
            y.value = withSpring(0);
            return;
          }
          runOnJS(fling)(direction);
        }),
    [fling, x, y],
  );

  const cardStyle = useAnimatedStyle(() => ({
    transform: [
      { translateX: x.value },
      { translateY: y.value },
      { rotate: `${x.value / 25}deg` },
    ],
  }));

  if (loading) {
    return (
      <SafeAreaView style={styles.screen} edges={['top', 'bottom']}>
        <View style={styles.centre}>
          <ActivityIndicator color={color.muted} />
        </View>
      </SafeAreaView>
    );
  }

  const card = deck[index];
  const finished = !card;

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'bottom']}>
      <View style={styles.header}>
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Back to the week"
          hitSlop={12}
          onPress={() => (router.canGoBack() ? router.back() : router.replace('/digest'))}
        >
          <Text style={styles.back}>Back</Text>
        </Pressable>
        <Text style={styles.progress}>
          {finished ? 'Done' : `${index + 1} of ${deck.length}`}
        </Text>
      </View>

      {error ? <Text style={styles.error}>{error}</Text> : null}

      {finished ? (
        <View style={styles.centre}>
          <Text style={styles.finishedTitle}>
            {deck.length ? 'That is the week.' : 'Nothing to review.'}
          </Text>
          <Text style={styles.finishedBody}>
            {deck.length
              ? `${deck.length} ${deck.length === 1 ? 'card' : 'cards'}${
                  kept ? `, ${kept} brought back` : ''
                }.`
              : 'Nothing decayed and nothing is near dropping.'}
          </Text>
          {failures.length ? (
            // Named rather than counted: "1 did not save" is not something you
            // can act on, and the whole point of holding these back was to let
            // the run finish first.
            <Text style={styles.failed}>
              {failures.length} did not save: {failures.join('; ')}
            </Text>
          ) : null}
        </View>
      ) : (
        <View style={styles.deck}>
          {/* The next card, showing just enough behind the top one that the
              deck reads as a deck — the count alone does not convey "nearly
              through" the way a thinning stack does. */}
          {deck[index + 1] ? (
            <View style={[styles.card, styles.behind]} pointerEvents="none">
              <Text style={styles.cardText} numberOfLines={4}>
                {deck[index + 1].text}
              </Text>
            </View>
          ) : null}

          <GestureDetector gesture={pan}>
            <Animated.View
              style={[styles.card, cardStyle]}
              accessibilityLabel={`${card.text}. ${dueAgeLabel(card.dueAt) || 'No due date'}.`}
            >
              <Text style={styles.cardText}>{card.text}</Text>
              <Ages card={card} />
            </Animated.View>
          </GestureDetector>
        </View>
      )}

      {!finished ? (
        <View style={styles.buttons}>
          {BUTTONS.map((direction) => (
            <Pressable
              key={direction}
              accessibilityRole="button"
              accessibilityHint={HINT[direction]}
              accessibilityLabel={DECISIONS[direction].label}
              onPress={() => fling(direction)}
              style={({ pressed }) => [styles.button, pressed && styles.pressed]}
            >
              <Text style={styles.arrow}>{ARROW[direction]}</Text>
              <Text style={styles.buttonText}>{DECISIONS[direction].label}</Text>
            </Pressable>
          ))}
        </View>
      ) : null}
    </SafeAreaView>
  );
}

export { RouteError as ErrorBoundary } from '../lib/RouteError';

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  centre: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    paddingHorizontal: space.lg,
    gap: space.sm,
  },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingHorizontal: space.md,
    paddingTop: space.sm,
    paddingBottom: space.sm,
  },
  back: { color: color.muted, fontSize: 15 },
  progress: { color: color.faint, fontSize: 14 },
  deck: { flex: 1, alignItems: 'center', justifyContent: 'center', padding: space.lg },
  card: {
    position: 'absolute',
    left: space.lg,
    right: space.lg,
    backgroundColor: color.surface,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    paddingHorizontal: space.lg,
    paddingVertical: space.lg,
    gap: space.md,
    minHeight: 200,
    justifyContent: 'center',
  },
  behind: { transform: [{ scale: 0.96 }, { translateY: 10 }], opacity: 0.5 },
  cardText: { color: color.text, fontSize: 20, lineHeight: 28 },
  ages: { gap: 2 },
  urgent: { color: color.overdue, fontSize: 14, fontWeight: '600' },
  age: { color: color.faint, fontSize: 14 },
  buttons: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    gap: space.sm,
    paddingHorizontal: space.md,
    paddingBottom: space.md,
  },
  button: {
    flex: 1,
    alignItems: 'center',
    gap: 2,
    paddingVertical: space.sm,
    borderRadius: radius.md,
    borderWidth: 1,
    borderColor: color.border,
    backgroundColor: color.surface,
  },
  pressed: { opacity: 0.6 },
  arrow: { color: color.muted, fontSize: 18 },
  buttonText: { color: color.muted, fontSize: 13 },
  error: { color: color.danger, fontSize: 14, paddingHorizontal: space.md },
  finishedTitle: { color: color.text, fontSize: 22, fontWeight: '600' },
  finishedBody: { color: color.muted, fontSize: 15, textAlign: 'center' },
  failed: { color: color.danger, fontSize: 14, textAlign: 'center', paddingTop: space.sm },
});
