/**
 * The weekly review deck (UC30) — its contents, and what a swipe means.
 *
 * Kept out of the screen because both halves are decisions rather than
 * rendering, and because a gesture is the one thing a test cannot make.
 *
 * **What is in the deck.** Only the two parts of the digest that carry a
 * decision: what decayed this week, and what is about to be dropped. What was
 * *completed* and what already *dropped* are terminal — there is nothing left
 * to swipe — so they stay on the digest as summary. Reading them is the point
 * of them.
 *
 * That bound is not a nicety. `PLAN.md` says the Sunday review takes under two
 * minutes, and `CLAUDE.md` says a feature that makes you do admin to keep
 * state accurate is the wrong feature. A deck of "everything on the shelf"
 * would be ninety days of accumulation and would fail both — it is the wall
 * that `Today` is bounded to avoid, dealt one card at a time.
 *
 * **What a swipe means.** Four directions, four states (UC30), and the mapping
 * is chosen so the two destructive ones are the two you have to be deliberate
 * about:
 *
 * ```
 *                  ↑  done
 *   shelved  ←   card   →  active
 *                  ↓  dropped
 * ```
 *
 * Right is forward — bring it back. Left is sideways — leave it where it is,
 * which is the only direction that asks nothing of the server, because for
 * every card in this deck "shelved" is already true. Up lifts it off. Down
 * bins it.
 *
 * Left being a no-op is the honest reading, not a shortcut: for something on
 * the expiring half, "leave it" means letting it drop on schedule. Touching
 * the row to record the non-decision would restart its ninety-day clock (D37)
 * and quietly turn "I looked at this and did nothing" into "keep it another
 * three months", which is the opposite of what the gesture said.
 */
import type { DigestResponse, ItemState, MovedItem, ExpiringItem } from './api';

/** Which half of the digest a card came from, and therefore how it reads. */
export type CardSource = 'decayed' | 'expiring';

/** One card in the deck, flattened from the two shapes the digest returns. */
export type ReviewCard = {
  id: string;
  text: string;
  kind: 'task' | 'note' | 'person_note';
  source: CardSource;
  /** When it was originally due, if it ever was. */
  dueAt: string | null;
  /** When the system shelved it — `decayed` cards only. */
  shelvedAt: string | null;
  /** Last touched, and when it will be dropped — `expiring` cards only. */
  untouchedSince: string | null;
  dropsAt: string | null;
};

function fromDecayed(item: MovedItem): ReviewCard {
  return {
    id: item.id,
    text: item.text,
    kind: item.kind,
    source: 'decayed',
    dueAt: item.due_at,
    shelvedAt: item.at,
    untouchedSince: null,
    dropsAt: null,
  };
}

function fromExpiring(item: ExpiringItem): ReviewCard {
  return {
    id: item.id,
    text: item.text,
    kind: item.kind,
    source: 'expiring',
    dueAt: item.due_at,
    shelvedAt: null,
    untouchedSince: item.untouched_since,
    dropsAt: item.drops_at,
  };
}

/**
 * The deck, in the order it is dealt.
 *
 * Expiring first, and that ordering is the same argument as the digest
 * screen's: those are the cards with a deadline on the decision. If the review
 * is abandoned halfway — which two minutes of anything usually is — the cards
 * that got looked at should be the ones that were about to disappear.
 *
 * An item can be in both halves at once: shelved by decay this week *and*
 * already close to its drop date, which happens to anything that was
 * reactivated near the end of its ninety days. It is dealt once, as the
 * expiring card, because that is the version of it with the deadline.
 */
export function buildDeck(week: DigestResponse): ReviewCard[] {
  const expiring = week.expiring.map(fromExpiring);
  const seen = new Set(expiring.map((card) => card.id));
  const decayed = week.shelved.filter((item) => !seen.has(item.id)).map(fromDecayed);
  return [...expiring, ...decayed];
}

/** The four ways off a card. */
export type Direction = 'up' | 'down' | 'left' | 'right';

/** What a swipe does, once it has been committed. */
export type Decision = {
  /** Where the card is being sent. */
  state: ItemState;
  /** What the card says as it goes, and what the legend calls the direction. */
  label: string;
  /**
   * Whether this asks anything of the server. False for `left`: every card in
   * this deck is already shelved, so "leave it" is a state the item is in.
   */
  writes: boolean;
};

export const DECISIONS: Record<Direction, Decision> = {
  up: { state: 'done', label: 'Done', writes: true },
  right: { state: 'active', label: 'Keep', writes: true },
  down: { state: 'dropped', label: 'Drop', writes: true },
  left: { state: 'shelved', label: 'Leave', writes: false },
};

/**
 * Which way a drag went, or null if it did not go far enough to count.
 *
 * The dominant axis wins outright rather than being blended: a drag is either
 * a horizontal answer or a vertical one, and a diagonal that resolved to
 * whichever component happened to be larger by two pixels would make `done`
 * and `keep` interchangeable at the corner. Ties go to the horizontal, because
 * left and right are the two reversible answers.
 *
 * @param dx Horizontal distance travelled.
 * @param dy Vertical distance travelled, positive downwards.
 * @param threshold How far a drag has to go to be an answer.
 */
export function directionOf(dx: number, dy: number, threshold: number): Direction | null {
  if (Math.abs(dx) >= Math.abs(dy)) {
    if (Math.abs(dx) < threshold) return null;
    return dx > 0 ? 'right' : 'left';
  }
  if (Math.abs(dy) < threshold) return null;
  return dy > 0 ? 'down' : 'up';
}
