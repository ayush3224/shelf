/**
 * Test environment for the app's library code.
 *
 * The two things every suite needs and neither Jest nor jest-expo provides:
 * the `EXPO_PUBLIC_*` values `lib/config` refuses to start without, and a
 * `fetch` that fails loudly instead of reaching the network.
 */

process.env.EXPO_PUBLIC_API_BASE_URL ??= 'https://api.test.invalid/api';
process.env.EXPO_PUBLIC_SUPABASE_URL ??= 'https://supabase.test.invalid';
process.env.EXPO_PUBLIC_SUPABASE_ANON_KEY ??= 'test-anon-key';

beforeEach(() => {
  // Any suite that needs a real response replaces this. An unreplaced call is
  // a test reaching for the network, which should fail as a bug, not hang.
  globalThis.fetch = jest.fn(async () => {
    throw new Error('Unexpected network call in a test');
  }) as unknown as typeof fetch;
});
