/**
 * Capture (UC5) — the launch screen (D9).
 *
 * Two taps at most: the field takes focus on mount, so it is type-and-send.
 * The acknowledgement says where the item landed, not what the model thought
 * it said — echoing the parse back taxes every single capture, and the place
 * to correct a bad parse is `Today` (O3).
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  ActivityIndicator,
  KeyboardAvoidingView,
  Platform,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
} from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiError, capture } from '../../lib/api';
import type { CaptureResponse } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { color, radius, space } from '../../lib/theme';

const NOTICE_MS = 6000;

/** Where the capture went, in one line. State is announced, never silent. */
function landedMessage(result: CaptureResponse): string {
  if (result.parse_status !== 'ok') {
    return "Saved. Couldn't read it — it's on the shelf, with your words kept.";
  }
  return result.state === 'active' ? "Saved — it's on Today." : 'Saved to the shelf.';
}

export default function Capture() {
  const { signOut } = useAuth();
  const inputRef = useRef<TextInput>(null);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  const submit = useCallback(async () => {
    const body = text.trim();
    if (!body || sending) return;

    setSending(true);
    setError(null);
    setNotice(null);
    try {
      const result = await capture(body);
      // Only cleared once the server has it. A failed send that wiped the box
      // would lose the capture, which is the one thing that must not happen.
      setText('');
      setNotice(landedMessage(result));
      inputRef.current?.focus();
    } catch (e) {
      if (e instanceof ApiError && e.isAuthError) {
        await signOut();
        return;
      }
      setError(
        e instanceof ApiError
          ? `${e.message} Your words are still here — try again.`
          : 'Something went wrong. Your words are still here — try again.',
      );
    } finally {
      setSending(false);
    }
  }, [text, sending, signOut]);

  const ready = text.trim().length > 0 && !sending;

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right']}>
      <KeyboardAvoidingView
        style={styles.fill}
        behavior={Platform.OS === 'ios' ? 'padding' : undefined}
      >
        <View style={styles.header}>
          <Text style={styles.wordmark}>Shelf</Text>
          <Pressable
            accessibilityRole="button"
            hitSlop={12}
            onPress={() => void signOut()}
          >
            <Text style={styles.signOut}>Sign out</Text>
          </Pressable>
        </View>

        <TextInput
          ref={inputRef}
          style={styles.input}
          value={text}
          onChangeText={setText}
          placeholder="What's on your mind?"
          placeholderTextColor={color.faint}
          multiline
          autoFocus
          autoCorrect
          editable={!sending}
          textAlignVertical="top"
          accessibilityLabel="Capture text"
        />

        <View style={styles.footer}>
          <View style={styles.messages}>
            {error ? (
              <Text style={styles.error}>{error}</Text>
            ) : notice ? (
              <Text style={styles.notice}>{notice}</Text>
            ) : null}
          </View>

          <Pressable
            accessibilityRole="button"
            accessibilityLabel="Capture"
            accessibilityState={{ disabled: !ready }}
            disabled={!ready}
            onPress={() => void submit()}
            style={({ pressed }) => [
              styles.button,
              !ready && styles.buttonDisabled,
              pressed && ready && styles.buttonPressed,
            ]}
          >
            {sending ? (
              <ActivityIndicator color={color.accentText} />
            ) : (
              <Text style={[styles.buttonLabel, !ready && styles.buttonLabelDisabled]}>
                Capture
              </Text>
            )}
          </Pressable>
        </View>
      </KeyboardAvoidingView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  fill: { flex: 1, paddingHorizontal: space.lg },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingTop: space.sm,
    paddingBottom: space.md,
  },
  wordmark: { fontSize: 20, fontWeight: '700', color: color.text, letterSpacing: -0.4 },
  signOut: { fontSize: 14, color: color.faint },
  input: {
    flex: 1,
    fontSize: 22,
    lineHeight: 32,
    color: color.text,
    paddingTop: space.sm,
  },
  footer: { paddingBottom: space.md },
  messages: { minHeight: 40, justifyContent: 'center' },
  notice: { fontSize: 14, lineHeight: 20, color: color.muted },
  error: { fontSize: 14, lineHeight: 20, color: color.danger },
  button: {
    backgroundColor: color.accent,
    borderRadius: radius.pill,
    height: 52,
    alignItems: 'center',
    justifyContent: 'center',
  },
  buttonPressed: { opacity: 0.85 },
  buttonDisabled: { backgroundColor: color.border },
  buttonLabel: { color: color.accentText, fontSize: 16, fontWeight: '600' },
  buttonLabelDisabled: { color: color.faint },
});
