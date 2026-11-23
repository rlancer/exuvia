import { useCallback, useEffect, useRef, useState } from "react";

/** Copy text to the clipboard; `copied` says so for a moment after. A refused clipboard leaves the text to copy by hand. */
export function useCopy(): [copied: boolean, copy: (text: string) => Promise<void>] {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  useEffect(() => () => void (timer.current && clearTimeout(timer.current)), []);
  const copy = useCallback(async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      if (timer.current) clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), 1500);
    } catch {
      /* no clipboard here: the field is selectable */
    }
  }, []);
  return [copied, copy];
}
