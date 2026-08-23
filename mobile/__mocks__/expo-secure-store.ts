/**
 * An in-memory stand-in for the device keystore.
 *
 * The size limit is the point. `expo-secure-store` rejects oversized values on
 * the real platforms, which is the whole reason `lib/secureStorage` chunks —
 * a mock without a ceiling would let a regression that stops chunking pass.
 */
export const KEYSTORE_VALUE_LIMIT = 2048;

const store = new Map<string, string>();

export function __reset(): void {
  store.clear();
}

export function __keys(): string[] {
  return [...store.keys()];
}

export function __raw(key: string): string | undefined {
  return store.get(key);
}

export async function getItemAsync(key: string): Promise<string | null> {
  return store.has(key) ? (store.get(key) as string) : null;
}

export async function setItemAsync(key: string, value: string): Promise<void> {
  if (value.length > KEYSTORE_VALUE_LIMIT) {
    throw new Error(
      `SecureStore value too large: ${value.length} > ${KEYSTORE_VALUE_LIMIT}`,
    );
  }
  store.set(key, value);
}

export async function deleteItemAsync(key: string): Promise<void> {
  store.delete(key);
}
