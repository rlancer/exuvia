import { describe, expect, it } from "vitest";
import { hostSlug, mcpSetup } from "./mcp";

const base = { origin: "https://exuvia.example.com", host: "exuvia.example.com" };

describe("MCP setup for a hub", () => {
  it("keeps the hub's shared token in a file, read by the local relay", () => {
    const s = mcpSetup({ ...base, auth: true });
    expect(s.server).toBe("exuvia");
    expect(s.url).toBe("https://exuvia.example.com");
    expect(s.endpoint).toBeNull();
    expect(s.codexToken).toBeNull();
    expect(s.claude).toBe(
      'claude mcp add exuvia --scope user --env EXUVIA_API_TOKEN_FILE="$HOME/.config/exuvia/exuvia.example.com.token" -- uvx exuvia@latest mcp --url https://exuvia.example.com',
    );
    expect(s.codex).toBe(
      'codex mcp add exuvia --env EXUVIA_API_TOKEN_FILE="$HOME/.config/exuvia/exuvia.example.com.token" -- uvx exuvia@latest mcp --url https://exuvia.example.com',
    );
    expect(JSON.parse(s.json)).toEqual({
      mcpServers: {
        exuvia: {
          command: "uvx",
          args: ["exuvia@latest", "mcp", "--url", "https://exuvia.example.com"],
          env: { EXUVIA_API_TOKEN_FILE: "~/.config/exuvia/exuvia.example.com.token" },
        },
      },
    });
    expect(s.saveToken).toBe("mkdir -p ~/.config/exuvia && (umask 077 && cat > ~/.config/exuvia/exuvia.example.com.token)");
    expect(s.prompt).toContain("every project on it");
    expect(s.prompt).toContain("EXUVIA_API_TOKEN_FILE to ~/.config/exuvia/exuvia.example.com.token");
    expect(s.prompt).toContain("never print it");
    expect(s.prompt).toContain("`uvx exuvia@latest mcp --url https://exuvia.example.com`");
  });

  it("connects straight to the hub's /mcp when the hub has no token", () => {
    const s = mcpSetup({ ...base, auth: false });
    expect(s.endpoint).toBe("https://exuvia.example.com/mcp");
    expect(s.tokenFile).toBeNull();
    expect(s.saveToken).toBeNull();
    expect(s.codexToken).toBeNull();
    expect(s.claude).toBe("claude mcp add --transport http --scope user exuvia https://exuvia.example.com/mcp");
    expect(s.codex).toBe("codex mcp add exuvia --url https://exuvia.example.com/mcp");
    expect(JSON.parse(s.json)).toEqual({ mcpServers: { exuvia: { type: "http", url: "https://exuvia.example.com/mcp" } } });
    expect(s.prompt).not.toContain("TOKEN");
    expect(s.prompt).not.toContain("uvx");
  });

  it("puts a person's agent token in place, so the commands paste as they are", () => {
    const token = "exv_AbC-d_123";
    const s = mcpSetup({ ...base, auth: true, token });
    expect(s.endpoint).toBe("https://exuvia.example.com/mcp");
    expect(s.tokenFile).toBeNull();
    expect(s.saveToken).toBeNull();
    // --header after the name and the URL: it takes several values.
    expect(s.claude).toBe(
      'claude mcp add --transport http --scope user exuvia https://exuvia.example.com/mcp --header "Authorization: Bearer exv_AbC-d_123"',
    );
    // Codex takes a bearer token only from an environment variable.
    expect(s.codex).toBe("codex mcp add exuvia --url https://exuvia.example.com/mcp --bearer-token-env-var EXUVIA_API_TOKEN");
    expect(s.codexToken).toEqual({ posix: "export EXUVIA_API_TOKEN=exv_AbC-d_123", windows: "setx EXUVIA_API_TOKEN exv_AbC-d_123" });
    expect(JSON.parse(s.json)).toEqual({
      mcpServers: { exuvia: { type: "http", url: "https://exuvia.example.com/mcp", headers: { Authorization: `Bearer ${token}` } } },
    });
    expect(s.prompt).toContain(`Authorization: Bearer ${token}`);
    expect(s.prompt).toContain("https://exuvia.example.com/mcp");
    expect(s.prompt).not.toContain("TOKEN_FILE");
    expect(s.prompt).not.toContain("uvx");
    // A hub with no shared token still needs the person's token.
    expect(mcpSetup({ ...base, auth: false, token }).claude).toContain("Bearer exv_AbC-d_123");
  });

  it("only takes agent tokens, which need no quoting", () => {
    expect(() => mcpSetup({ ...base, auth: true, token: "exv_a; rm -rf ~" })).toThrow();
    expect(() => mcpSetup({ ...base, auth: true, token: "s3cret" })).toThrow();
  });

  it("names token files after the hub's host", () => {
    expect(hostSlug("localhost:7420")).toBe("localhost-7420");
    expect(hostSlug("Exuvia.Example.com")).toBe("exuvia.example.com");
  });
});
