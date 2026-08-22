/**
 * Capture first, always (D9).
 *
 * `index` is the capture screen, so the app opens on it and `Today` is one tap
 * away rather than the thing you land in.
 */
import { Tabs } from 'expo-router';

import { color } from '../../lib/theme';

export default function TabsLayout() {
  return (
    <Tabs
      screenOptions={{
        headerShown: false,
        tabBarActiveTintColor: color.text,
        tabBarInactiveTintColor: color.faint,
        tabBarLabelStyle: { fontSize: 13, fontWeight: '600' },
        tabBarStyle: {
          backgroundColor: color.surface,
          borderTopColor: color.border,
          height: 60,
          paddingTop: 6,
        },
        tabBarIconStyle: { display: 'none' },
      }}
    >
      <Tabs.Screen name="index" options={{ title: 'Capture' }} />
      <Tabs.Screen name="today" options={{ title: 'Today' }} />
    </Tabs>
  );
}
