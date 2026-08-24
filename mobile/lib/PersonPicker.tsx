/**
 * Pick a person — or name one that does not exist yet.
 *
 * Both halves of manual correction need the same gesture, so they share the
 * same sheet: a merge picks who to fold in (UC48), a split picks where notes
 * are going (UC49). The only difference is whether naming somebody new is on
 * offer — you cannot merge into a person who has no notes, because there would
 * be nothing to fold.
 *
 * The search field doubles as the name field. Typing "Priya Nair" filters the
 * list to whoever matches, and if nobody does, the same text becomes the offer
 * to create them. One box, so there is never a moment of deciding which of two
 * inputs to type into — and the create row only appears once the text matches
 * nobody exactly, so the common case of picking an existing person is never
 * shadowed by an offer to duplicate them.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ActivityIndicator,
  FlatList,
  Modal,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiError, people as fetchPeople } from './api';
import type { Person } from './api';
import { color, radius, space } from './theme';

/** Long enough that typing a name is one request, short enough to feel live. */
const SEARCH_DEBOUNCE_MS = 250;

export type PersonPickerProps = {
  visible: boolean;
  /** Shown at the top. Says what picking somebody is about to do. */
  title: string;
  /** Explains the consequence in one line, since neither action is obvious. */
  subtitle?: string;
  /** Whoever the sheet was opened from, hidden from the list. */
  excludeId?: string;
  /** Whether naming somebody new is on offer. False for a merge. */
  allowCreate?: boolean;
  onPick: (choice: { id: string } | { name: string }) => void;
  onCancel: () => void;
};

/** Compare the way the server does, so "create" never offers a duplicate. */
function normalised(name: string): string {
  return name.trim().replace(/\s+/g, ' ').toLowerCase();
}

export function PersonPicker({
  visible,
  title,
  subtitle,
  excludeId,
  allowCreate = false,
  onPick,
  onCancel,
}: PersonPickerProps) {
  const [term, setTerm] = useState('');
  const [search, setSearch] = useState('');
  const [rows, setRows] = useState<Person[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const generation = useRef(0);

  // Opening the sheet is a fresh question; keeping the last one's text would
  // silently filter the list to something the user has forgotten they typed.
  useEffect(() => {
    if (!visible) return;
    setTerm('');
    setSearch('');
    setError(null);
  }, [visible]);

  useEffect(() => {
    const next = term.trim();
    if (next === search) return;
    const timer = setTimeout(() => setSearch(next), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [term, search]);

  const load = useCallback(async () => {
    const mine = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const data = await fetchPeople(search || undefined);
      if (generation.current !== mine) return;
      setRows(data.people);
    } catch (e) {
      if (generation.current !== mine) return;
      setError(e instanceof ApiError ? e.message : 'Could not load people.');
    } finally {
      if (generation.current === mine) setLoading(false);
    }
  }, [search]);

  useEffect(() => {
    if (visible) void load();
  }, [visible, load]);

  const choices = useMemo(
    () => rows.filter((p) => p.id !== excludeId),
    [rows, excludeId],
  );

  const typed = term.trim();
  const exact = choices.some((p) => normalised(p.name) === normalised(typed));
  const offerCreate = allowCreate && typed.length > 0 && !exact;

  return (
    <Modal
      visible={visible}
      animationType="slide"
      transparent={false}
      onRequestClose={onCancel}
    >
      <SafeAreaView style={styles.screen} edges={['top', 'left', 'right', 'bottom']}>
        <View style={styles.header}>
          <View style={styles.headerText}>
            <Text style={styles.title}>{title}</Text>
            {subtitle ? <Text style={styles.subtitle}>{subtitle}</Text> : null}
          </View>
          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Cancel"
            hitSlop={12}
            onPress={onCancel}
            style={({ pressed }) => [styles.cancel, pressed && styles.pressed]}
          >
            <Text style={styles.cancelText}>Cancel</Text>
          </Pressable>
        </View>

        <TextInput
          style={styles.search}
          value={term}
          onChangeText={setTerm}
          placeholder={allowCreate ? 'Search, or type a new name' : 'Search people'}
          placeholderTextColor={color.faint}
          accessibilityLabel="Search or name a person"
          autoCapitalize="words"
          autoCorrect={false}
          autoFocus
          returnKeyType="done"
        />

        {error ? <Text style={styles.error}>{error}</Text> : null}

        {loading && choices.length === 0 ? (
          <View style={styles.centered}>
            <ActivityIndicator color={color.muted} />
          </View>
        ) : (
          <FlatList
            data={choices}
            keyExtractor={(p) => p.id}
            keyboardShouldPersistTaps="handled"
            renderItem={({ item }) => (
              <Pressable
                accessibilityRole="button"
                accessibilityLabel={item.name}
                onPress={() => onPick({ id: item.id })}
                style={({ pressed }) => [styles.row, pressed && styles.pressed]}
              >
                <View style={styles.rowBody}>
                  <Text style={styles.name}>{item.name}</Text>
                  {item.aliases.length ? (
                    <Text style={styles.aka}>also {item.aliases.join(', ')}</Text>
                  ) : null}
                </View>
                <Text style={styles.count}>{item.mentions}</Text>
              </Pressable>
            )}
            ListHeaderComponent={
              offerCreate ? (
                <Pressable
                  accessibilityRole="button"
                  accessibilityLabel={`Create ${typed}`}
                  onPress={() => onPick({ name: typed })}
                  style={({ pressed }) => [
                    styles.row,
                    styles.createRow,
                    pressed && styles.pressed,
                  ]}
                >
                  <Text style={styles.createText}>New person “{typed}”</Text>
                </Pressable>
              ) : null
            }
            contentContainerStyle={styles.list}
            ListEmptyComponent={
              offerCreate ? null : (
                <View style={styles.empty}>
                  <Text style={styles.emptyBody}>
                    {allowCreate
                      ? 'Nobody by that name yet — type one to create them.'
                      : 'Nobody else to pick.'}
                  </Text>
                </View>
              )
            }
          />
        )}
      </SafeAreaView>
    </Modal>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  centered: { flex: 1, alignItems: 'center', justifyContent: 'center' },
  header: {
    flexDirection: 'row',
    alignItems: 'flex-start',
    gap: space.md,
    paddingHorizontal: space.lg,
    paddingTop: space.md,
    paddingBottom: space.md,
  },
  headerText: { flex: 1, gap: space.xs },
  title: { fontSize: 22, fontWeight: '700', color: color.text, letterSpacing: -0.4 },
  subtitle: { fontSize: 14, lineHeight: 20, color: color.muted },
  cancel: { paddingVertical: space.xs },
  cancelText: { fontSize: 15, color: color.muted },
  pressed: { opacity: 0.6 },
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
  rowBody: { flex: 1, gap: 2 },
  name: { fontSize: 17, color: color.text, fontWeight: '600' },
  aka: { fontSize: 13, color: color.faint },
  count: { fontSize: 15, fontWeight: '600', color: color.muted },
  createRow: { borderColor: color.accent, borderStyle: 'dashed' },
  createText: { fontSize: 16, color: color.accent, fontWeight: '600' },
  empty: { paddingTop: space.xl, alignItems: 'center' },
  emptyBody: {
    fontSize: 15,
    lineHeight: 22,
    color: color.muted,
    textAlign: 'center',
  },
});
