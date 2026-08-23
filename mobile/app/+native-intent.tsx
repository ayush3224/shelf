/**
 * Deep links that are not routes.
 *
 * On Android `WebBrowser.openAuthSessionAsync` intercepts nothing. It has no
 * native auth-session implementation there (`_authSessionIsNativelySupported`
 * returns `Platform.OS !== 'android'`), so it falls back to a polyfill built on
 * a plain `Linking.addEventListener('url', ...)`. The OAuth redirect therefore
 * arrives as an ordinary Android intent on the `shelf` scheme and *every*
 * subscriber to that event sees it: expo-web-browser's one-shot handler, and
 * expo-router's router, which subscribes to the very same event.
 *
 * So the sign-in succeeds and the router simultaneously tries to navigate to
 * `/auth-callback`, finds no such route, and renders "Unmatched route" over the
 * top of it. The URL is a transport detail, not a screen, so it is swallowed
 * here rather than given a route of its own: returning a falsy path tells
 * expo-router to stay where it is. Nothing needs to navigate to `(tabs)` by
 * hand — the session landing flips the guards in `app/_layout.tsx`.
 */
import { completeAuthRedirect, isAuthCallbackUrl } from '../lib/auth';

/**
 * Bound on the cold-start exchange, so a dead network costs a re-tap rather
 * than a splash screen that never resolves. React Native's `fetch` has no
 * timeout of its own and expo-router waits on this before rendering.
 */
const COLD_START_EXCHANGE_TIMEOUT_MS = 15_000;

/**
 * Race `work` against the clock, always clearing the timer.
 *
 * The clear matters: without it the winner leaves a live timer behind that
 * fires into nothing 15 seconds later, holding its closure the whole time.
 */
async function withTimeout(work: Promise<unknown>, ms: number): Promise<void> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    await Promise.race([
      work,
      new Promise<void>((resolve) => {
        timer = setTimeout(resolve, ms);
      }),
    ]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export async function redirectSystemPath({
  path,
  initial,
}: {
  path: string;
  initial: boolean;
}): Promise<string | null> {
  if (!isAuthCallbackUrl(path)) return path;

  // A warm redirect is already awaited by the `openAuthSessionAsync` call in
  // `signInWithGoogle`, which owns the exchange, the error message and the
  // spinner. Exchanging here as well would race it for a single-use code and
  // one of the two would lose: the winner deletes the PKCE verifier, so the
  // loser fails with a missing-verifier error and reports a sign-in that
  // actually worked as broken.
  //
  // A cold start has no such caller. If Android killed the process while the
  // Custom Tab was in front, that promise and its listener died with it and
  // the redirect arrives as the app's initial URL instead. The verifier is in
  // the keystore, so the exchange is still possible, and this is the only
  // place left that can do it.
  if (initial) {
    // expo-router's docs are explicit that throwing here can crash the app.
    await withTimeout(
      completeAuthRedirect(path).catch(() => undefined),
      COLD_START_EXCHANGE_TIMEOUT_MS,
    );
  }

  return null;
}
