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
import { color } from '../lib/theme';

// Warming the browser makes the Google hand-off feel instant instead of blank.
void WebBrowser.warmUpAsync();

function RootNavigator() {
  const { session, loading } = useAuth();

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
        {/* Item detail sits outside the tabs: it is reached from a row, and
            giving it a tab would make it a place rather than a detour. */}
        <Stack.Screen name="item/[id]" />
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

const styles = StyleSheet.create({
  splash: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: color.bg,
  },
});
