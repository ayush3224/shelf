/**
 * Taking an item off somebody's page, and the one question it has to ask.
 *
 * An unlink is a correction to the filing, not to the capture: the words, the
 * recording and the item all survive it, and linking them back undoes it. That
 * is why neither entry point confirms — the chip on item detail (UC45) or the
 * row on the person page (UC46).
 *
 * There is exactly one exception, and it is why this module exists rather than
 * the screens each calling `removeItemPerson`. Emptying a person removes them
 * (the rule a split follows, UC49), and removing them discards the names they
 * go by. Those aliases are the accumulated record of resolutions that came out
 * right — a bare "Priya" filed onto "Priya Sharma", a name folded in by a
 * merge — and relinking the item does not bring them back. D45 lets the
 * matching guess *because* the owner can correct it; silently throwing away
 * the corrections is the one move that spends that licence without asking.
 *
 * **The server decides, not this.** It answers 409 rather than emptying an
 * alias-bearing person, so a screen holding a stale mention count cannot slip
 * the removal past. This asks, then repeats the request saying so. A person
 * with no aliases holds nothing but a name the next mention recreates, so that
 * case never reaches a dialog.
 */
import { Alert } from 'react-native';

import { ApiError, removeItemPerson } from './api';
import type { ItemPeopleResponse, LinkedPerson } from './api';

/** Just enough of a person to unlink them and to say what it would cost. */
export type UnlinkTarget = Pick<LinkedPerson, 'id' | 'name'> & {
  aliases: string[];
};

/** `Alert` as something that can be awaited, so the caller reads as a sequence. */
function ask(who: UnlinkTarget): Promise<boolean> {
  const names = who.aliases.map((alias) => `“${alias}”`).join(', ');
  return new Promise((resolve) => {
    Alert.alert(
      `Remove ${who.name} too?`,
      `That is the last thing on their page, so ${who.name} goes with it — and so do the names you taught them: ${names}. Nothing you said is deleted.`,
      [
        { text: 'Cancel', style: 'cancel', onPress: () => resolve(false) },
        {
          text: 'Remove',
          style: 'destructive',
          onPress: () => resolve(true),
        },
      ],
      { cancelable: true, onDismiss: () => resolve(false) },
    );
  });
}

/**
 * Take an item off a person's page, asking first only where it cannot be undone.
 *
 * @param itemId The item to detach.
 * @param who The person to detach it from, with the names they go by.
 * @returns What the item is linked to afterwards, or null if the owner
 *   cancelled the removal — in which case nothing changed at all.
 */
export async function unlinkPerson(
  itemId: string,
  who: UnlinkTarget,
): Promise<ItemPeopleResponse | null> {
  try {
    return await removeItemPerson(itemId, who.id);
  } catch (e) {
    // 409 is the server refusing to discard the aliases unasked. Every other
    // failure is a failure and belongs to the caller.
    if (!(e instanceof ApiError) || e.status !== 409) throw e;
    if (!(await ask(who))) return null;
    return await removeItemPerson(itemId, who.id, { removePerson: true });
  }
}
