/**
 * How to connect a coding agent to a hub over MCP: the hub's home page shows
 * these. One server covers every project (list_projects, create_project, and
 * each project tool with a `project` argument).
 *
 * The hub is an MCP server itself, at `<hub>/mcp` over Streamable HTTP
 * (exuvia/mcp_http.py), so a client needs only that address and, where the
 * hub has auth, a bearer token. On a hub where people sign in, each person
 * makes an agent token of their own (`token`): it goes into the config as it
 * is, so the commands are copied and pasted with nothing else to set up (Codex
 * reads it from an environment variable), and the agent's work is recorded as
 * theirs. It can be revoked any time.
 *
 * With the hub's shared API token instead, the token stays out of every
 * config: the client runs the local relay (`uvx exuvia mcp --url`, exuvia/
 * mcp_server.py), which reads it from a file only the user can read
 * (EXUVIA_API_TOKEN_FILE; `~` there is the home directory, exuvia/cli.py).
 * HTTP clients have no portable way to read a header from a file. The same
 * file name is in the README's deploy prompt.
 */
export interface McpSetup {
  /** The MCP server's name in the client. */
  server: string;
  /** The hub's address. */
  url: string;
  /** The hub's own MCP endpoint, when the client connects to it directly; null: through the local relay. */
  endpoint: string | null;
  /** Codex reads a bearer token from an environment variable: how to set it (POSIX shells, Windows). */
  codexToken: { posix: string; windows: string } | null;
  /** Where the token file goes; null when the hub needs no token, or with an agent token. */
  tokenFile: string | null;
  /** A shell command that saves the token there (paste it, Enter, Ctrl-D). */
  saveToken: string | null;
  /** A prompt for any coding agent that sets itself up. */
  prompt: string;
  claude: string;
  codex: string;
  /** `mcpServers` JSON for Cursor, a project's .mcp.json and the like. */
  json: string;
}

/** `exuvia.example.com` or `localhost-7420`: a file name for the hub's token. */
export const hostSlug = (host: string) => host.toLowerCase().replace(/[^a-z0-9.-]+/g, "-");

/** Agent tokens are `exv_` and URL-safe base64: nothing a shell or JSON needs quoted. */
const AGENT_TOKEN = /^exv_[A-Za-z0-9_-]+$/;

export function mcpSetup(o: { origin: string; host: string; auth: boolean; token?: string }): McpSetup {
  const server = "exuvia";
  const url = o.origin;
  if (o.token !== undefined && !AGENT_TOKEN.test(o.token)) throw new Error("not an agent token");
  const token = o.token;
  // Straight to the hub's /mcp unless the shared token has to come from a file.
  if (token || !o.auth) return direct(server, url, token);

  const tokenFile = `~/.config/exuvia/${hostSlug(o.host)}.token`;
  // In a shell, `~` after `=` is not expanded; $HOME is (inside double quotes).
  const envVar = `EXUVIA_API_TOKEN_FILE="$HOME${tokenFile.slice(1)}"`;
  // @latest: a plain `uvx exuvia` runs an exuvia installed with `uv tool
  // install` when there is one, whatever its version, and hub-wide MCP is new.
  const command = ["uvx", "exuvia@latest", "mcp", "--url", url];

  const prompt = [
    `Connect yourself over MCP to the exuvia hub at ${url}, so you have its tools natively from your next session on: every project on it (list_projects, create_project, and each project's tools).`,
    `Add an MCP server named ${server} to your own configuration, with your CLI's own command if it has one, such as \`claude mcp add\` (with --scope user), \`codex mcp add\` or \`grok mcp add\`.`,
    `The server's command: \`${command.join(" ")}\` (\`pipx run exuvia mcp --url ${url}\` without uv).`,
    `Set the environment variable EXUVIA_API_TOKEN_FILE to ${tokenFile}: that file holds the hub's API token and only I can read it.`,
    `If it doesn't exist, ask me to create it; never put the token itself in the configuration and never print it.`,
    `Then check that its \`list_projects\` tool answers, and tell me what you added.`,
  ].join(" ");

  return {
    server,
    url,
    endpoint: null,
    codexToken: null,
    tokenFile,
    saveToken: `mkdir -p ~/.config/exuvia && (umask 077 && cat > ${tokenFile})`,
    prompt,
    // The name goes first: `--env` takes several values and would swallow it.
    // --scope user: from any folder, not only the one the command ran in.
    claude: `claude mcp add ${server} --scope user --env ${envVar} -- ${command.join(" ")}`,
    codex: `codex mcp add ${server} --env ${envVar} -- ${command.join(" ")}`,
    json: JSON.stringify(
      { mcpServers: { [server]: { command: command[0], args: command.slice(1), env: { EXUVIA_API_TOKEN_FILE: tokenFile } } } },
      null,
      2,
    ),
  };
}

/** A client of the hub's own MCP endpoint, with a person's agent token or none. */
function direct(server: string, url: string, token: string | undefined): McpSetup {
  const endpoint = `${url}/mcp`;
  const bearer = token && `Authorization: Bearer ${token}`;
  const prompt = [
    `Connect yourself over MCP to the exuvia hub at ${url}, so you have its tools natively from your next session on: every project on it (list_projects, create_project, and each project's tools).`,
    `It is a remote MCP server (Streamable HTTP) at ${endpoint}: add it to your own configuration as a server named ${server}, with your CLI's own command if it has one, such as \`claude mcp add --transport http --scope user\` or \`codex mcp add ${server} --url ${endpoint}\`. Nothing needs installing.`,
    ...(token
      ? [
          `Send the header \`${bearer}\` with every request: it is my personal agent token for this hub, so the work is recorded as mine.`,
          `If your client only takes the token from an environment variable (Codex: \`--bearer-token-env-var EXUVIA_API_TOKEN\`), tell me to set EXUVIA_API_TOKEN to it myself.`,
          `Don't print it or put it anywhere else.`,
        ]
      : []),
    `Then check that its \`list_projects\` tool answers, and tell me what you added.`,
  ].join(" ");
  return {
    server,
    url,
    endpoint,
    codexToken: token ? { posix: `export EXUVIA_API_TOKEN=${token}`, windows: `setx EXUVIA_API_TOKEN ${token}` } : null,
    tokenFile: null,
    saveToken: null,
    prompt,
    // --header goes last: it takes several values and would swallow the name.
    // --scope user: from any folder, not only the one the command ran in.
    claude: `claude mcp add --transport http --scope user ${server} ${endpoint}${bearer ? ` --header "${bearer}"` : ""}`,
    codex: `codex mcp add ${server} --url ${endpoint}${token ? " --bearer-token-env-var EXUVIA_API_TOKEN" : ""}`,
    json: JSON.stringify(
      { mcpServers: { [server]: { type: "http", url: endpoint, ...(bearer ? { headers: { Authorization: `Bearer ${token}` } } : {}) } } },
      null,
      2,
    ),
  };
}
