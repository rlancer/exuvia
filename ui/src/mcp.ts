/**
 * How to connect a coding agent to a hub over MCP: the hub's home page shows
 * these. One server covers every project (exuvia/mcp_server.py HubBackend:
 * list_projects, create_project, and each project tool with a `project`
 * argument). The token never appears: clients read it from a file only the
 * user can read (EXUVIA_API_TOKEN_FILE; `~` there is the home directory,
 * exuvia/cli.py). The same file name is in the README's deploy prompt.
 *
 * On a hub where people sign in, each person makes an agent token of their
 * own instead (`token`): it goes into the config as it is, so the commands
 * are copied and pasted with nothing else to set up, and the agent's work is
 * recorded as theirs. It can be revoked any time.
 */
export interface McpSetup {
  /** The MCP server's name in the client. */
  server: string;
  url: string;
  /** Where the token file goes; null when the hub needs no token, or with an agent token. */
  tokenFile: string | null;
  /** A shell command that saves the token there (paste it, Enter, Ctrl-D). */
  saveToken: string | null;
  /** A prompt for any coding agent that sets itself up. */
  prompt: string;
  claude: string;
  codex: string;
  /** `mcpServers` JSON for Cursor, Claude Desktop, Windsurf and the like. */
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
  const tokenFile = o.auth && !token ? `~/.config/exuvia/${hostSlug(o.host)}.token` : null;
  // In a shell, `~` after `=` is not expanded; $HOME is (inside double quotes).
  const shellFile = tokenFile && `"$HOME${tokenFile.slice(1)}"`;
  const envVar = token ? `EXUVIA_API_TOKEN=${token}` : shellFile ? `EXUVIA_API_TOKEN_FILE=${shellFile}` : null;
  const env = (flag: string) => (envVar ? ` ${flag} ${envVar}` : "");
  // @latest: a plain `uvx exuvia` runs an exuvia installed with `uv tool
  // install` when there is one, whatever its version, and hub-wide MCP is new.
  const command = ["uvx", "exuvia@latest", "mcp", "--url", url];

  const prompt = [
    `Connect yourself over MCP to the exuvia hub at ${url}, so you have its tools natively from your next session on: every project on it (list_projects, create_project, and each project's tools).`,
    `Add an MCP server named ${server} to your own configuration, with your CLI's own command if it has one, such as \`claude mcp add\` (with --scope user), \`codex mcp add\` or \`grok mcp add\`.`,
    `The server's command: \`${command.join(" ")}\` (\`pipx run exuvia mcp --url ${url}\` without uv).`,
    ...(token
      ? [
          `Set the environment variable EXUVIA_API_TOKEN to ${token} in the server's configuration: it is my personal agent token for this hub, so the work is recorded as mine.`,
          `Don't print it or put it anywhere else.`,
        ]
      : tokenFile
      ? [
          `Set the environment variable EXUVIA_API_TOKEN_FILE to ${tokenFile}: that file holds the hub's API token and only I can read it.`,
          `If it doesn't exist, ask me to create it; never put the token itself in the configuration and never print it.`,
        ]
      : []),
    `Then check that its \`list_projects\` tool answers, and tell me what you added.`,
  ].join(" ");

  return {
    server,
    url,
    tokenFile,
    saveToken: tokenFile && `mkdir -p ~/.config/exuvia && (umask 077 && cat > ${tokenFile})`,
    prompt,
    // The name goes first: `--env` takes several values and would swallow it.
    // --scope user: from any folder, not only the one the command ran in.
    claude: `claude mcp add ${server} --scope user${env("--env")} -- ${command.join(" ")}`,
    codex: `codex mcp add ${server}${env("--env")} -- ${command.join(" ")}`,
    json: JSON.stringify(
      {
        mcpServers: {
          [server]: {
            command: command[0],
            args: command.slice(1),
            ...(token ? { env: { EXUVIA_API_TOKEN: token } } : tokenFile ? { env: { EXUVIA_API_TOKEN_FILE: tokenFile } } : {}),
          },
        },
      },
      null,
      2,
    ),
  };
}
