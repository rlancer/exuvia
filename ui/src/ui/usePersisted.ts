import { useCallback, useRef, useState } from "react";

/*
 * What this browser remembers for the app: a panel left open, a tab, a
 * layout per project. Values are kept as they always were, so what an earlier
 * version stored still reads: a string as itself, anything else as JSON.
 */

/** A value as it is stored. */
export function encode(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value);
}

/** A stored value, if `accept` takes it (it returns the value to use, or undefined): JSON when it parses, else the text itself. */
export function decode<T>(raw: string | null, accept: (v: unknown) => T | undefined): T | undefined {
  if (raw === null) return undefined;
  let v: unknown = raw;
  try {
    v = JSON.parse(raw);
  } catch {
    /* a plain string */
  }
  return accept(v) ?? (v !== raw ? accept(raw) : undefined);
}

/** Read `key`; private mode, a full store or no key at all read as nothing. */
export function readStored<T>(key: string | null, accept: (v: unknown) => T | undefined): T | undefined {
  if (!key) return undefined;
  try {
    return decode(localStorage.getItem(key), accept);
  } catch {
    return undefined;
  }
}

/** Write `key`, if there is one and the browser lets it. */
export function writeStored(key: string | null, value: unknown) {
  if (!key) return;
  try {
    localStorage.setItem(key, encode(value));
  } catch {
    /* private mode */
  }
}

/**
 * State this browser remembers under `key`: read when the key changes (a
 * project's name arriving, another project), written on every change. What is
 * stored and not taken by `accept` reads as `fallback`. A null key remembers
 * nothing.
 */
export function usePersisted<T>(
  key: string | null,
  fallback: T | (() => T),
  accept: (v: unknown) => T | undefined,
): [T, (next: T | ((old: T) => T)) => void] {
  const read = () => readStored(key, accept) ?? (typeof fallback === "function" ? (fallback as () => T)() : fallback);
  const [held, setHeld] = useState(() => ({ key, value: read() }));
  let { value } = held;
  if (held.key !== key) {
    // Another key: what is stored under it shows in this same render, not one
    // late (a remembered layout must not be drawn as the default first).
    value = read();
    setHeld({ key, value });
  }
  const keyRef = useRef(key);
  keyRef.current = key;
  const set = useCallback(
    (next: T | ((old: T) => T)) =>
      setHeld((old) => {
        const v = typeof next === "function" ? (next as (old: T) => T)(old.value) : next;
        writeStored(keyRef.current, v);
        return { key: keyRef.current, value: v };
      }),
    [],
  );
  return [value, set];
}

/** An `accept` for one of a few strings. */
export const oneOf =
  <S extends string>(...values: S[]) =>
  (v: unknown): S | undefined =>
    values.includes(v as S) ? (v as S) : undefined;
