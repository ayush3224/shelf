/**
 * What a route shows when it fails to render.
 *
 * expo-router picks this up by name: a route or layout file that exports
 * `ErrorBoundary` is wrapped in `<Try catch={...}>`, and anything thrown while
 * rendering that segment lands here with `error` and a `retry`.
 *
 * It exists because of how the tab bar bug behaved (D41). Nothing threw — the
 * bar rendered at six device-independent pixels and was simply invisible — so
 * no boundary would have caught *that*. What made it expensive was the
 * category of failure it belongs to: **the app degrading into something that
 * still looks like a working screen.** A layout that returns null, throws, or
 * collapses leaves you looking at a plausible app with a piece missing, and
 * you go hunting for a routing bug that is not there.
 *
 * So this screen is deliberately loud and deliberately specific. It names the
 * segment, prints the message and the component stack, and offers a retry. A
 * boundary that renders a shrug is worse than none — it converts a crash you
 * could have diagnosed into a mystery.
 */
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { color, radius, space } from './theme';

export type RouteErrorProps = {
  /** What was thrown while rendering the segment. */
  error: Error;
  /** Re-mount the segment. Worth offering: a failed fetch on mount often passes. */
  retry: () => Promise<void>;
};

/** The `componentStack` React attaches, when there is one. */
function componentStack(error: Error): string | null {
  const stack = (error as Error & { componentStack?: unknown }).componentStack;
  return typeof stack === 'string' && stack.trim() ? stack.trim() : null;
}

export function RouteError({ error, retry }: RouteErrorProps) {
  const stack = componentStack(error) ?? error.stack ?? null;

  return (
    <SafeAreaView style={styles.screen} edges={['top', 'left', 'right', 'bottom']}>
      <ScrollView contentContainerStyle={styles.body}>
        <Text style={styles.title}>This screen failed to render.</Text>
        <Text style={styles.lead}>
          The rest of the app is fine — this is one screen, caught on the way up.
        </Text>

        <Text style={styles.label}>What went wrong</Text>
        <Text style={styles.message}>
          {`${error.name}: ${error.message || '(no message)'}`}
        </Text>

        {stack ? (
          <>
            <Text style={styles.label}>Where</Text>
            {/* Scrolls rather than truncates: the first frame is rarely the
                interesting one, and a stack cut off at the screen edge is the
                same as no stack. */}
            <ScrollView horizontal style={styles.stackWrap}>
              <Text style={styles.stack}>{stack}</Text>
            </ScrollView>
          </>
        ) : null}

        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Try rendering this screen again"
          onPress={() => void retry()}
          style={({ pressed }) => [styles.button, pressed && styles.pressed]}
        >
          <Text style={styles.buttonText}>Try again</Text>
        </Pressable>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg },
  body: { padding: space.lg, gap: space.sm },
  title: { fontSize: 22, fontWeight: '700', color: color.text, letterSpacing: -0.4 },
  lead: { fontSize: 15, lineHeight: 22, color: color.muted, marginBottom: space.md },
  label: {
    fontSize: 11,
    fontWeight: '700',
    letterSpacing: 0.8,
    textTransform: 'uppercase',
    color: color.faint,
    marginTop: space.md,
  },
  message: {
    fontSize: 15,
    lineHeight: 22,
    color: color.danger,
    fontFamily: 'monospace',
  },
  stackWrap: {
    maxHeight: 260,
    backgroundColor: color.surface,
    borderWidth: 1,
    borderColor: color.border,
    borderRadius: radius.sm,
    padding: space.sm,
  },
  stack: { fontSize: 11, lineHeight: 16, color: color.muted, fontFamily: 'monospace' },
  button: {
    marginTop: space.lg,
    alignSelf: 'flex-start',
    paddingHorizontal: space.lg,
    paddingVertical: space.sm + 2,
    borderRadius: radius.pill,
    backgroundColor: color.accent,
  },
  pressed: { opacity: 0.8 },
  buttonText: { fontSize: 15, fontWeight: '600', color: color.accentText },
});
