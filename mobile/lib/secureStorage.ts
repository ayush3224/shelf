/**
 * A Supabase storage adapter backed by the device keystore.
 *
 * `expo-secure-store` writes to the Android Keystore / iOS keychain, which is
 * the point — the session it holds is a bearer token for the whole API. The
 * catch is that the platforms reject large values (historically ~2KB on iOS),
 * and a Supabase session carrying two JWTs and a user object clears that
 * comfortably. So values are split across numbered keys and reassembled on
 * read. Anything else silently loses the session on a token refresh.
 */
import * as SecureStore from 'expo-secure-store';

/** Well under the platform limits, with room for the key name itself. */
const CHUNK_SIZE = 1536;

type Manifest = { chunks: number };

function chunkKey(key: string, index: number): string {
  return `${key}.${index}`;
}

function parseManifest(raw: string): Manifest | null {
  try {
    const parsed: unknown = JSON.parse(raw);
    if (
      typeof parsed === 'object' &&
      parsed !== null &&
      typeof (parsed as Manifest).chunks === 'number'
    ) {
      return parsed as Manifest;
    }
  } catch {
    // Not a manifest. Older writes stored the value inline.
  }
  return null;
}

/**
 * Read a key, ignoring the read error rather than propagating it.
 *
 * A keychain entry written under different device settings throws instead of
 * returning null. Treating that as "absent" costs a re-login; letting it throw
 * takes the whole app down at startup.
 */
async function readRaw(key: string): Promise<string | null> {
  try {
    return await SecureStore.getItemAsync(key);
  } catch {
    return null;
  }
}

async function deleteChunks(key: string, count: number): Promise<void> {
  await Promise.all(
    Array.from({ length: count }, (_, i) =>
      SecureStore.deleteItemAsync(chunkKey(key, i)).catch(() => undefined),
    ),
  );
}

export const secureStorage = {
  async getItem(key: string): Promise<string | null> {
    const head = await readRaw(key);
    if (head === null) return null;

    const manifest = parseManifest(head);
    if (!manifest) return head;

    const parts = await Promise.all(
      Array.from({ length: manifest.chunks }, (_, i) => readRaw(chunkKey(key, i))),
    );
    // A missing chunk means a half-written value; a partial session is worse
    // than none, because it fails later and somewhere less obvious.
    if (parts.some((p) => p === null)) return null;
    return parts.join('');
  },

  async setItem(key: string, value: string): Promise<void> {
    const previous = await readRaw(key);
    const stale = previous ? parseManifest(previous)?.chunks ?? 0 : 0;

    const chunks: string[] = [];
    for (let i = 0; i < value.length; i += CHUNK_SIZE) {
      chunks.push(value.slice(i, i + CHUNK_SIZE));
    }

    await Promise.all(
      chunks.map((part, i) => SecureStore.setItemAsync(chunkKey(key, i), part)),
    );
    // The manifest lands last: until it does, a reader sees the old value
    // rather than a torn one.
    await SecureStore.setItemAsync(key, JSON.stringify({ chunks: chunks.length }));

    if (stale > chunks.length) {
      await Promise.all(
        Array.from({ length: stale - chunks.length }, (_, i) =>
          SecureStore.deleteItemAsync(chunkKey(key, chunks.length + i)).catch(
            () => undefined,
          ),
        ),
      );
    }
  },

  async removeItem(key: string): Promise<void> {
    const head = await readRaw(key);
    const manifest = head ? parseManifest(head) : null;
    await SecureStore.deleteItemAsync(key).catch(() => undefined);
    if (manifest) await deleteChunks(key, manifest.chunks);
  },
};
