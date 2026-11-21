import { useEffect, useState } from "react";
import { me, type Me } from "./api";

// One `GET /api/me` per page: the top bar, its account menu and the connect
// dialog all ask who this is.
let asked: Promise<Me | null> | null = null;

/** Who is signed in: undefined while asking, null on an older server. */
export function useMe(): Me | null | undefined {
  const [who, setWho] = useState<Me | null | undefined>(undefined);
  useEffect(() => {
    asked ??= me().catch(() => null);
    let live = true;
    asked.then((m) => live && setWho(m));
    return () => {
      live = false;
    };
  }, []);
  return who;
}
