import { useEffect, useState } from "react";
import { AlertDialog } from "@astryxdesign/core/AlertDialog";
import { useSignedOut } from "./session";

/** How long "Not now" keeps the dialog away while the page stays signed out, in ms. */
const SNOOZE = 60_000;

/**
 * Says the page has been signed out (src/session.ts): its data can't be read
 * until the person signs in again, which a reload of the page does (the
 * sign-in proxy, or the hub's token form, takes over). "Not now" leaves the
 * page as it is, to copy something out of it; the dialog comes back a
 * minute later if the page is still signed out.
 */
export function SignedOut() {
  const signedOut = useSignedOut();
  const [snoozedAt, setSnoozedAt] = useState<number | null>(null);
  const [, wake] = useState(0);
  // Signed in again (another tab): the next lapse asks at once.
  useEffect(() => {
    if (!signedOut) setSnoozedAt(null);
  }, [signedOut]);
  // Asked again once the snooze is over, if still signed out.
  useEffect(() => {
    if (snoozedAt === null) return;
    const t = window.setTimeout(() => wake((n) => n + 1), Math.max(0, snoozedAt + SNOOZE - Date.now()));
    return () => window.clearTimeout(t);
  }, [snoozedAt]);
  const snoozed = snoozedAt !== null && Date.now() < snoozedAt + SNOOZE;
  return (
    <AlertDialog
      isOpen={signedOut && !snoozed}
      onOpenChange={(open) => {
        if (!open) setSnoozedAt(Date.now());
      }}
      title="You've been signed out"
      description="Your sign-in has ended, so exuvia can't load data or save changes. Sign in again to pick up where you left off; anything not yet saved on this page may be lost when it reloads."
      cancelLabel="Not now"
      actionLabel="Sign in again"
      actionVariant="primary"
      onAction={() => location.reload()}
    />
  );
}
