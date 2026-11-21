import { useEffect, useState } from "react";
import { me, type Me } from "./api";

/** The person a sign-in proxy says this is (EXUVIA_AUTH=headers), for the top bar. */
export function SignedInAs() {
  const [who, setWho] = useState<Me["user"]>(null);
  useEffect(() => {
    me()
      .then((m) => setWho(m.user))
      .catch(() => setWho(null)); // an older server, or not signed in
  }, []);
  if (!who) return null;
  return (
    <span className="signed-in" title={who.email && who.email !== who.id ? `${who.id} · ${who.email}` : who.id}>
      {who.id}
    </span>
  );
}
