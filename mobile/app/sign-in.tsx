/** Google sign-in (UC41). The only screen that exists without a session. */
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native';
import { SafeAreaView } from 'react-native-safe-area-context';

import { useAuth } from '../lib/auth';
import { color, radius, space } from '../lib/theme';

export default function SignIn() {
  const { signInWithGoogle, signingIn, error } = useAuth();

  return (
    <SafeAreaView style={styles.screen}>
      <View style={styles.body}>
        <Text style={styles.wordmark}>Shelf</Text>
        <Text style={styles.tagline}>
          Say it once. It decides what it is and when to bring it back.
        </Text>
      </View>

      <View style={styles.footer}>
        {error ? <Text style={styles.error}>{error}</Text> : null}
        <Pressable
          accessibilityRole="button"
          accessibilityLabel="Continue with Google"
          disabled={signingIn}
          onPress={() => void signInWithGoogle()}
          style={({ pressed }) => [
            styles.button,
            pressed && styles.buttonPressed,
            signingIn && styles.buttonDisabled,
          ]}
        >
          {signingIn ? (
            <ActivityIndicator color={color.accentText} />
          ) : (
            <Text style={styles.buttonLabel}>Continue with Google</Text>
          )}
        </Pressable>
      </View>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  screen: { flex: 1, backgroundColor: color.bg, paddingHorizontal: space.lg },
  body: { flex: 1, justifyContent: 'flex-end', paddingBottom: space.lg },
  wordmark: { fontSize: 40, fontWeight: '700', color: color.text, letterSpacing: -1 },
  tagline: {
    marginTop: space.sm,
    fontSize: 16,
    lineHeight: 24,
    color: color.muted,
    maxWidth: 300,
  },
  footer: { flex: 1, justifyContent: 'center' },
  error: {
    color: color.danger,
    fontSize: 14,
    lineHeight: 20,
    marginBottom: space.md,
  },
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
  buttonDisabled: { opacity: 0.6 },
  buttonLabel: { color: color.accentText, fontSize: 16, fontWeight: '600' },
});
