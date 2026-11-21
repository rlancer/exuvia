import { describe, expect, it } from "vitest";
import { hostSlug, mcpSetup } from "./mcp";

const base = { origin: "https://exuvia.example.com", host: "exuvia.example.com" };

describe("MCP setup for a hub", () => {
  it("is one server for the whole hub", () => {
    const s = mcpSetup({ ...base, auth: true });
    expect(s.server).toBe("exuvia");
    expect(s.url).toBe("https://exuvia.example.com");
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

  it("leaves the token out when the hub has none", () => {
    const s = mcpSetup({ ...base, auth: false });
    expect(s.tokenFile).toBeNull();
    expect(s.saveToken).toBeNull();
    expect(s.claude).toBe("claude mcp add exuvia --scope user -- uvx exuvia@latest mcp --url https://exuvia.example.com");
    expect(JSON.parse(s.json).mcpServers.exuvia.env).toBeUndefined();
    expect(s.prompt).not.toContain("TOKEN");
  });

  it("puts a person's agent token in place, so the commands paste as they are", () => {
    const token = "exv_AbC-d_123";
    const s = mcpSetup({ ...base, auth: true, token });
    expect(s.tokenFile).toBeNull();
    expect(s.saveToken).toBeNull();
    expect(s.claude).toBe(
      "claude mcp add exuvia --scope user --env EXUVIA_API_TOKEN=exv_AbC-d_123 -- uvx exuvia@latest mcp --url https://exuvia.example.com",
    );
    expect(s.codex).toBe("codex mcp add exuvia --env EXUVIA_API_TOKEN=exv_AbC-d_123 -- uvx exuvia@latest mcp --url https://exuvia.example.com");
    expect(JSON.parse(s.json).mcpServers.exuvia.env).toEqual({ EXUVIA_API_TOKEN: token });
    expect(s.prompt).toContain(`EXUVIA_API_TOKEN to ${token}`);
    expect(s.prompt).not.toContain("TOKEN_FILE");
    // A hub with no shared token still needs the person's token.
    expect(mcpSetup({ ...base, auth: false, token }).claude).toContain("EXUVIA_API_TOKEN=exv_AbC-d_123");
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
