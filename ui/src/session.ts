import { useSyncExternalStore } from "react";

/*
 * Whether this page is still signed in. Once a sign-in lapses (the proxy's
 * session ends, the token cookie goes, a share link is revoked), every API
 * call answers 401, and most reads fail quietly: a table's next page, a
 * preview, the 30-second refresh. Each answer the page gets is noted here
 * (`noteAnswer`), so the page can say it was signed out (src/SignedOut.tsx)
 * instead of going blank.
 *
 * A 401 counts only after the page has been answered at least once: a page
 * that was never signed in (the hub asking for its token) says so itself.
 */

let answered = false;
let signedOut = false;
const listeners = new Set<() => void>();

const set = (next: boolean) => {
  if (signedOut === next) return;
  signedOut = next;
  listeners.forEach((l) => l());
};

/** Note the status of an answer from the API: a 401 after an answer means the sign-in has lapsed. */
export function noteAnswer(status: number) {
  if (status === 401) {
    if (answered) set(true);
    return;
  }
  // Any other answer means the server still knows who this is (signed in again in another tab, say).
  answered = true;
  if (status < 400) set(false);
}

/** Whether the page has been signed out since it loaded. */
export function useSignedOut(): boolean {
  return useSyncExternalStore(
    (l) => {
      listeners.add(l);
      return () => void listeners.delete(l);
    },
    () => signedOut,
  );
}

/** Whether the page is signed out, outside React. */
export const signedOutNow = () => signedOut;

/** For tests: back to a page that has not been answered yet. */
export function resetSession() {
  answered = false;
  signedOut = false;
}
