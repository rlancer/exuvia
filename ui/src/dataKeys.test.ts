import { describe, expect, it } from "vitest";
import { agentPrompt, curlExample, specUrl } from "./DataKeys";

const HUB = "https://hub.example.com";

describe("data API key handoff", () => {
  it("points at the hub's own data API spec", () => {
    expect(specUrl(HUB)).toBe("https://hub.example.com/api/data/openapi.json");
  });

  it("gives an agent the address, the key, the projects and the spec to start from", () => {
    const p = agentPrompt(HUB, "exd_secret", ["sales", "crm"]);
    expect(p).toContain("- Address: https://hub.example.com/api/data/");
    expect(p).toContain("- Key: exd_secret");
    expect(p).toContain("Authorization: Bearer <key>");
    expect(p).toContain("Projects it reads: sales, crm.");
    expect(p).toContain('"sales".<node>');
    expect(p).toContain("Fetch https://hub.example.com/api/data/openapi.json (no key needed)");
    expect(p).toContain("https://exuvia.lobster.mp/docs/hub.md");
    expect(p).toMatch(/don't|out of code you commit/);
  });

  it("tries the key with curl against the same hub", () => {
    const c = curlExample(HUB, "exd_secret", ["sales"]);
    expect(c).toContain("curl -s https://hub.example.com/api/data/projects -H \"Authorization: Bearer exd_secret\"");
    expect(c).toContain('from \\"sales\\".<node>');
  });
});
