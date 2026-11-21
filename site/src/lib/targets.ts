// The deploy targets the README's agent steps cover, in the order the README
// and the landing page list them. A module of its own (no file reads) so the
// landing page's picker script can import it too.

/** `label` is how the README's deploy steps mark a target's own lines (`- Compose: …`). */
export const DEPLOY_TARGETS = [
  { id: "compose", name: "Docker Compose", label: "Compose" },
  { id: "swarm", name: "Docker Swarm", label: "Swarm" },
  { id: "kubernetes", name: "Kubernetes", label: "Kubernetes" },
] as const;

/** The hostname the README's deploy prompt shows, which the landing page swaps for the reader's. */
export const EXAMPLE_HOSTNAME = "exuvia.example.com";
