/**
 * Browser storage namespaced by the signed-in user.
 *
 * Two people can share one browser profile, and everything this app keeps
 * in localStorage -- the query cache, the intent ledger, compose recovery,
 * view and search preferences, shortcut bindings, the push subscription id
 * -- describes one mailbox. Every such key carries the user's id as a
 * suffix (`<key>@<user id>`), so one person's state is never read, written
 * or replayed under another's.
 *
 * The id comes from `/api/me`. `UserGate` (components/user-gate.tsx) binds
 * it before anything persisted is read; asking for a scoped key before that
 * throws instead of falling back to a shared one.
 */

import { atom, type WritableAtom } from "jotai";
import { createJSONStorage, RESET } from "jotai/utils";

const SCOPE_MARK = "@";

/** Key families this app writes. A key of one of these without a user
 * suffix predates namespacing and is removed on bind. */
const APP_KEY_PREFIXES = ["mailverdict:", "mail-verdict", "mv."];

/** The persisted query cache's key (see lib/query-persister.ts). */
export const QUERY_CACHE_KEY = "mail-verdict-query-cache";

/** Per-user entries that are only a cache of server state: another user's
 * copy is dropped when a different person signs in, so a shared browser
 * does not keep a previous person's mail lying around. */
const DISCARD_FOR_OTHER_USERS = [QUERY_CACHE_KEY];

let boundUserId: string | null = null;

/** The id bound by `bindUser`, or null before `/api/me` has answered. */
export function boundUser(): string | null {
  return boundUserId;
}

export function requireUserId(): string {
  if (boundUserId === null) {
    throw new Error("Persisted state was touched before the signed-in user was known");
  }
  return boundUserId;
}

export function scopedKeyFor(userId: string, key: string): string {
  return `${key}${SCOPE_MARK}${userId}`;
}

export function scopedKey(key: string): string {
  return scopedKeyFor(requireUserId(), key);
}

type KeyedStorage = Pick<Storage, "length" | "key" | "removeItem">;

/** Removes what this user must not inherit: unsuffixed keys from before
 * namespacing, and other users' cached server state. Returns the removed
 * keys. */
export function sweepStorage(storage: KeyedStorage, userId: string): string[] {
  const removed: string[] = [];
  for (let i = storage.length - 1; i >= 0; i--) {
    const key = storage.key(i);
    if (key === null || !APP_KEY_PREFIXES.some((p) => key.startsWith(p))) continue;
    const at = key.lastIndexOf(SCOPE_MARK);
    const legacy = at === -1;
    const otherUsersCache =
      !legacy &&
      key.slice(at + 1) !== userId &&
      DISCARD_FOR_OTHER_USERS.includes(key.slice(0, at));
    if (legacy || otherUsersCache) {
      storage.removeItem(key);
      removed.push(key);
    }
  }
  return removed;
}

/** Makes `userId` the owner of every scoped key from now on. The sweep is
 * best effort: a browser with storage disabled has nothing to leak. */
export function bindUser(userId: string, storage?: KeyedStorage): void {
  if (boundUserId === userId) return;
  boundUserId = userId;
  try {
    const target = storage ?? (typeof window !== "undefined" ? window.localStorage : undefined);
    if (target) sweepStorage(target, userId);
  } catch {
    // Storage unavailable (private mode, blocked): nothing persisted either.
  }
}

/** Test seam: forgets the bound user. */
export function unbindUser(): void {
  boundUserId = null;
}

type ValueStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

/** A view of `storage` whose every key is scoped to the bound user, for
 * code written against the plain Storage methods. */
export function scopedStorage(storage: ValueStorage): ValueStorage {
  return {
    getItem: (key) => storage.getItem(scopedKey(key)),
    setItem: (key, value) => storage.setItem(scopedKey(key), value),
    removeItem: (key) => storage.removeItem(scopedKey(key)),
  };
}

/** `window.localStorage` scoped to the bound user. */
export function userLocalStorage(): ValueStorage {
  return scopedStorage(window.localStorage);
}

/**
 * `atomWithStorage` over user-scoped keys.
 *
 * The stored value is read when the atom is first read or mounted, never at
 * module load -- the user is not known then. With `getOnInit` the first
 * read already returns the stored value (what virtua's mount-only `cache`
 * prop needs); without it the atom reads `initialValue` until it mounts.
 * Other tabs' writes under the same user's key arrive through the storage
 * event.
 */
export function userAtomWithStorage<T>(
  key: string,
  initialValue: T,
  options?: {
    getOnInit?: boolean;
    /** Maps what was read from storage (or `initialValue`) to the value used. */
    readTransform?: (stored: T) => T;
  },
): WritableAtom<T, [T | ((prev: T) => T) | typeof RESET], void> {
  const json = createJSONStorage<T>(() => window.localStorage);
  const transform = options?.readTransform ?? ((v: T) => v);
  const read = () => transform(json.getItem(scopedKey(key), initialValue));

  const base = atom<{ value: T } | null>(null);
  base.onMount = (setBase) => {
    setBase({ value: read() });
    return json.subscribe?.(
      scopedKey(key),
      (value) => setBase({ value: transform(value) }),
      initialValue,
    );
  };

  return atom(
    (get) => (get(base) ?? { value: options?.getOnInit ? read() : initialValue }).value,
    (get, set, update) => {
      const current = (get(base) ?? { value: options?.getOnInit ? read() : initialValue }).value;
      if (update === RESET) {
        set(base, { value: initialValue });
        json.removeItem(scopedKey(key));
        return;
      }
      const next = typeof update === "function" ? (update as (prev: T) => T)(current) : update;
      set(base, { value: next });
      json.setItem(scopedKey(key), next);
    },
  );
}

/** A writable atom whose first value is computed on first use rather than
 * at module load, for state seeded from user-scoped storage. */
export function lazyAtom<T>(
  init: () => T,
): WritableAtom<T, [T | ((prev: T) => T)], void> {
  const base = atom<{ value: T } | null>(null);
  return atom(
    (get) => (get(base) ?? { value: init() }).value,
    (get, set, update) => {
      const current = (get(base) ?? { value: init() }).value;
      set(base, {
        value: typeof update === "function" ? (update as (prev: T) => T)(current) : update,
      });
    },
  );
}
