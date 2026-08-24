/**
 * Capture first, always (D9).
 *
 * `index` is the capture screen, so the app opens on it and `Today` is one tap
 * away rather than the thing you land in.
 *
 * `Shelf` goes last on purpose. It is the archive (UC33) — the place you go
 * looking for something, not the place anything pushes you toward — and the
 * tab order is the only ranking of these three screens the app ever states.
 *
 * **The height has to include the bottom safe-area inset, and that is not a
 * detail (D41).** React Navigation's tab bar adds the inset itself — but only
 * on the path where it computes its own height. Give `tabBarStyle` a literal
 * `height` and it takes that number verbatim (`getTabBarHeight` returns early
 * on a numeric custom height) while *still* applying `paddingBottom:
 * insets.bottom` underneath it. A hardcoded 60 on a phone with gesture
 * navigation therefore left `60 - 6 - 48 = 6dp` for the labels: a white strip
 * the height of a hairline, on a near-white background, which reads as no tab
 * bar at all. That is what it did on the device from the app's first commit
 * until 24 August 2026 — every tab was unreachable and nothing said so,
 * because in a test the insets are zero and the same code is fine.
 */
import { Tabs } from 'expo-router';
import { useSafeAreaInsets } from 'react-native-safe-area-context';

import { color } from '../../lib/theme';

/**
 * Height of the bar's own content, before the inset.
 *
 * The inset is added to this rather than absorbed by it: the system gesture
 * area is not space the tab bar gets to use, so subtracting from a fixed total
 * is how the labels get squeezed out.
 */
const BAR_CONTENT_HEIGHT = 54;

/** Room above the labels. Paired with the inset below them, never instead of it. */
const BAR_PADDING_TOP = 6;

export default function TabsLayout() {
  const insets = useSafeAreaInsets();

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
          // Not a literal: see the note at the top of this file.
          height: BAR_CONTENT_HEIGHT + insets.bottom,
          paddingTop: BAR_PADDING_TOP,
        },
        tabBarIconStyle: { display: 'none' },
      }}
    >
      <Tabs.Screen name="index" options={{ title: 'Capture' }} />
      <Tabs.Screen name="today" options={{ title: 'Today' }} />
      <Tabs.Screen name="shelf" options={{ title: 'Shelf' }} />
    </Tabs>
  );
}

export { RouteError as ErrorBoundary } from '../../lib/RouteError';
