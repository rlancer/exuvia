import type { APIRoute, GetStaticPaths } from "astro";
import { agentFiles, type AgentFile } from "../../lib/docs";

// The steps the README's agent prompts send an agent to fetch: /agent/setup.txt
// and /agent/deploy/<target>.txt, cut from the README by lib/docs.ts. Plain
// text, so curl returns them word for word and a browser shows them as is.
export const getStaticPaths: GetStaticPaths = () =>
  agentFiles().map((file) => ({ params: { file: file.path.replace(/^\/agent\//, "") }, props: { file } }));

export const GET: APIRoute = ({ props }) =>
  new Response((props as { file: AgentFile }).file.text, {
    headers: { "Content-Type": "text/plain; charset=utf-8" },
  });
