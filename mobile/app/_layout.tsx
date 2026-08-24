/**
 * Root layout: session gate and nothing else.
 *
 * The tabs are behind a session; the sign-in screen is behind not having one.
 */
import { useEffect } from 'react';
import { ActivityIndicator, StyleSheet, View } from 'react-native';
import { SafeAreaProvider } from 'react-native-safe-area-context';
import { StatusBar } from 'expo-status-bar';
import { Stack } from 'expo-router';
import * as WebBrowser from 'expo-web-browser';

import { AuthProvider, useAuth } from '../lib/auth';
import { usePushNotifications } from '../lib/notifications';
import { color } from '../lib/theme';

// Warming the browser makes the Google hand-off feel instant instead of blank.
void WebBrowser.warmUpAsync();

function RootNavigator() {
  const { session, loading } = useAuth();

  // Registration and the Done/Snooze buttons (UC23, UC15, UC17). Mounted here
  // rather than on a screen: a push arrives whether or not the app is open,
  // and the response has to be handled wherever the app happens to be. It is
  // gated on the session because a push token is stored against a user.
  usePushNotifications(!!session);

  useEffect(() => {
    return () => {
      void WebBrowser.coolDownAsync();
    };
  }, []);

  if (loading) {
    return (
      <View style={styles.splash}>
        <ActivityIndicator color={color.muted} />
      </View>
    );
  }

  return (
    <Stack screenOptions={{ headerShown: false }}>
      <Stack.Protected guard={!!session}>
        <Stack.Screen name="(tabs)" />
        {/* Item detail and a person page both sit outside the tabs: each is
            reached from a row, and giving either a tab would make it a place
            rather than a detour. */}
        <Stack.Screen name="item/[id]" />
        <Stack.Screen name="person/[id]" />
        {/* The digest is the same kind of thing: somewhere a notification
            sends you once a week (UC31), not somewhere you live. Four tabs
            is the ceiling (D44) and this is not the fifth. */}
        <Stack.Screen name="digest" />
      </Stack.Protected>
      <Stack.Protected guard={!session}>
        <Stack.Screen name="sign-in" />
      </Stack.Protected>
    </Stack>
  );
}

export default function RootLayout() {
  return (
    <SafeAreaProvider>
      <StatusBar style="dark" />
      <AuthProvider>
        <RootNavigator />
      </AuthProvider>
    </SafeAreaProvider>
  );
}

// A render failure anywhere under the root lands here rather than leaving a
// plausible-looking app with a piece missing (D41).
export { RouteError as ErrorBoundary } from '../lib/RouteError';

const styles = StyleSheet.create({
  splash: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: color.bg,
  },
});
