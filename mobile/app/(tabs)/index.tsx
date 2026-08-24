/**
 * Capture (UC1, UC5) — the launch screen (D9).
 *
 * Voice is the primary action and it is one gesture: hold the mic, speak,
 * release. Typing is the fallback, one tap away, and becomes the only path if
 * the microphone is refused — a denied permission is a preference, not an
 * error, so the screen quietly rearranges itself around it rather than nagging.
 *
 * Permission is asked on the first hold, never at launch (see lib/recorder).
 *
 * The acknowledgement says where the item landed, not what the model thought
 * it heard — echoing the parse back taxes every single capture, and the place
 * to correct a bad parse is `Today` (O3). The wording of it lives in
 * `lib/landing`, because "where it landed" has to name a place that exists and
 * that turned out to be worth testing (D57).
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
import type { GestureResponderEvent } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { ApiError, capture, captureAudio } from '../../lib/api';
import type { CaptureResponse } from '../../lib/api';
import { useAuth } from '../../lib/auth';
import { landedMessage } from '../../lib/landing';
import { MIN_RECORDING_MS, useRecorder } from '../../lib/recorder';
import { color, radius, space } from '../../lib/theme';

const NOTICE_MS = 6000;

/** Drag this far from the mic and releasing throws the recording away. */
const CANCEL_DISTANCE = 80;

function seconds(ms: number): string {
  return `${Math.floor(ms / 1000)}s`;
}

/**
 * Say which machine the problem is on.
 *
 * The previous single message ("that recording did not reach the server") was
 * true of every failure and useful for none of them: it read as a network
 * fault whether the network was involved or not.
 */
function uploadFailureMessage(e: unknown): string {
  if (!(e instanceof ApiError)) {
    return `Something went wrong sending that recording. ${describeError(e)}`;
  }

  switch (e.kind) {
    case 'client':
      // Never dispatched. The recording is still on the device.
      return `The app could not send that recording — it is still here. ${e.diagnostic}`;
    case 'transport':
      return 'No connection. The recording is still here — try again.';
    case 'timeout':
      return 'The server took too long. The recording is still here — try again.';
    default:
      return e.status === 503
        ? `${e.message} Try again in a moment.`
        : `The server refused that recording (${e.status}). ${e.message}`;
  }
}

function describeError(e: unknown): string {
  return e instanceof Error ? `${e.name}: ${e.message}` : String(e);
}

export default function Capture() {
  const { signOut } = useAuth();
  const recorder = useRecorder();
  const inputRef = useRef<TextInput>(null);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [willCancel, setWillCancel] = useState(false);

  const pressOrigin = useRef<{ x: number; y: number } | null>(null);
  const cancelling = useRef(false);

  useEffect(() => {
    if (!notice) return;
    const timer = setTimeout(() => setNotice(null), NOTICE_MS);
    return () => clearTimeout(timer);
  }, [notice]);

  /** Shared tail of both capture paths. */
  const settle = useCallback(
    async (send: () => Promise<CaptureResponse>, onSent: () => void) => {
      setSending(true);
      setError(null);
      setNotice(null);
      try {
        const result = await send();
        onSent();
        setNotice(landedMessage(result));
      } catch (e) {
        if (e instanceof ApiError && e.isAuthError) {
          await signOut();
          return;
        }
        throw e;
      } finally {
        setSending(false);
      }
    },
    [signOut],
  );

  const submitText = useCallback(async () => {
    const body = text.trim();
    if (!body || sending) return;
    try {
      await settle(
        () => capture(body),
        // Only cleared once the server has it. A failed send that wiped the box
        // would lose the capture, which is the one thing that must not happen.
        () => {
          setText('');
          inputRef.current?.focus();
        },
      );
    } catch (e) {
      // Same classification as the audio path: a request that never left the
      // device should not be reported as the server being unreachable.
      const why =
        e instanceof ApiError && e.kind === 'client'
          ? `The app could not send that. ${e.diagnostic}`
          : e instanceof ApiError
            ? e.message
            : describeError(e);
      setError(`${why} Your words are still here — try again.`);
    }
  }, [text, sending, settle]);

  const beginRecording = useCallback(
    async (event: GestureResponderEvent) => {
      if (sending) return;
      cancelling.current = false;
      setWillCancel(false);
      pressOrigin.current = {
        x: event.nativeEvent.pageX,
        y: event.nativeEvent.pageY,
      };
      setNotice(null);
      setError(null);
      const started = await recorder.start();
      if (!started) {
        pressOrigin.current = null;
        // A refusal is not an error message; the screen has already swapped to
        // the typing path, so point at it instead.
        if (recorder.state === 'denied') {
          setNotice('No microphone access — type it instead.');
          inputRef.current?.focus();
        }
      }
    },
    [recorder, sending],
  );

  /** Track the finger so a drag away from the button means "throw it away". */
  const trackDrag = useCallback((event: GestureResponderEvent) => {
    const origin = pressOrigin.current;
    if (!origin) return;
    const dx = event.nativeEvent.pageX - origin.x;
    const dy = event.nativeEvent.pageY - origin.y;
    const far = Math.hypot(dx, dy) > CANCEL_DISTANCE;
    cancelling.current = far;
    setWillCancel(far);
  }, []);

  const endRecording = useCallback(async () => {
    pressOrigin.current = null;
    if (recorder.state !== 'recording') return;

    if (cancelling.current) {
      cancelling.current = false;
      setWillCancel(false);
      await recorder.cancel();
      setNotice('Discarded.');
      return;
    }

    const result = await recorder.stop();
    setWillCancel(false);

    if (result.outcome === 'too-short') {
      // A tap, not a hold. Say what the gesture is rather than failing.
      setNotice(
        `Hold the button while you speak — that was under ${MIN_RECORDING_MS / 1000}s.`,
      );
      return;
    }

    if (result.outcome === 'unusable') {
      // The recording never made it to disk. Nothing to do with the server,
      // and saying otherwise is what sent the last investigation to the VPS.
      setError(`${result.message} Nothing was sent.`);
      return;
    }

    try {
      await settle(
        () => captureAudio(result.recording),
        () => undefined,
      );
    } catch (e) {
      setError(uploadFailureMessage(e));
    }
  }, [recorder, settle]);

  const recording = recorder.state === 'recording';
  const ready = text.trim().length > 0 && !sending;
  const busy = sending || recorder.state === 'stopping';

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
          style={[styles.input, recorder.micUnavailable && styles.inputPrimary]}
          value={text}
          onChangeText={setText}
          placeholder={recorder.micUnavailable ? "What's on your mind?" : 'Or type it'}
          placeholderTextColor={color.faint}
          multiline
          // Not focused on mount: the mic is the primary action and a keyboard
          // over it would bury the thing the screen is for.
          autoFocus={recorder.micUnavailable}
          autoCorrect
          editable={!busy && !recording}
          textAlignVertical="top"
          accessibilityLabel="Capture text"
        />

        <View style={styles.footer}>
          <View style={styles.messages}>
            {error ? (
              <Text style={styles.error}>{error}</Text>
            ) : recording ? (
              <Text style={willCancel ? styles.error : styles.notice}>
                {willCancel
                  ? 'Release to discard'
                  : `Listening — ${seconds(recorder.durationMs)}`}
              </Text>
            ) : notice ? (
              <Text style={styles.notice}>{notice}</Text>
            ) : recorder.error ? (
              <Text style={styles.error}>{recorder.error}</Text>
            ) : null}
          </View>

          {recorder.micUnavailable ? null : (
            <View style={styles.micRow}>
              <Pressable
                accessibilityRole="button"
                accessibilityLabel="Hold to record"
                accessibilityHint="Press and hold while you speak, then release to save"
                accessibilityState={{ busy: recording, disabled: busy }}
                disabled={busy}
                onPressIn={(e) => void beginRecording(e)}
                onTouchMove={trackDrag}
                onPressOut={() => void endRecording()}
                style={[
                  styles.mic,
                  recording && styles.micRecording,
                  willCancel && styles.micCancelling,
                  busy && styles.micDisabled,
                ]}
              >
                {busy ? (
                  <ActivityIndicator color={color.accentText} />
                ) : (
                  <Text style={styles.micGlyph}>{recording ? '■' : '●'}</Text>
                )}
              </Pressable>
              <Text style={styles.micHint}>
                {recording ? 'Release to save' : 'Hold to record'}
              </Text>
            </View>
          )}

          {ready || recorder.micUnavailable ? (
            <Pressable
              accessibilityRole="button"
              accessibilityLabel="Capture"
              accessibilityState={{ disabled: !ready }}
              disabled={!ready}
              onPress={() => void submitText()}
              style={({ pressed }) => [
                styles.button,
                !ready && styles.buttonDisabled,
                pressed && ready && styles.buttonPressed,
              ]}
            >
              {sending && !recording ? (
                <ActivityIndicator color={color.accentText} />
              ) : (
                <Text style={[styles.buttonLabel, !ready && styles.buttonLabelDisabled]}>
                  Capture
                </Text>
              )}
            </Pressable>
          ) : null}
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
    fontSize: 18,
    lineHeight: 26,
    color: color.text,
    paddingTop: space.sm,
  },
  inputPrimary: { fontSize: 22, lineHeight: 32 },
  footer: { paddingBottom: space.md },
  messages: { minHeight: 40, justifyContent: 'center' },
  notice: { fontSize: 14, lineHeight: 20, color: color.muted },
  error: { fontSize: 14, lineHeight: 20, color: color.danger },
  micRow: { alignItems: 'center', paddingBottom: space.md },
  mic: {
    width: 88,
    height: 88,
    borderRadius: 44,
    backgroundColor: color.accent,
    alignItems: 'center',
    justifyContent: 'center',
  },
  micRecording: { backgroundColor: color.danger, transform: [{ scale: 1.06 }] },
  micCancelling: { backgroundColor: color.faint },
  micDisabled: { opacity: 0.6 },
  micGlyph: { color: color.accentText, fontSize: 30, lineHeight: 34 },
  micHint: { marginTop: space.sm, fontSize: 13, color: color.faint },
  button: {
    backgroundColor: color.accent,
    borderRadius: radius.pill,
    // A floor, not a height: 52 is the tap target we want, but the label has
    // to be able to push the box taller when the system font scale is turned
    // up. A literal `height` here clips it instead (D42).
    minHeight: 52,
    paddingVertical: space.sm,
    paddingHorizontal: space.md,
    alignItems: 'center',
    justifyContent: 'center',
  },
  buttonPressed: { opacity: 0.85 },
  buttonDisabled: { backgroundColor: color.border },
  buttonLabel: { color: color.accentText, fontSize: 16, fontWeight: '600' },
  buttonLabelDisabled: { color: color.faint },
});
