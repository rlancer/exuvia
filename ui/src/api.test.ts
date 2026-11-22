import { afterEach, describe, expect, it, vi } from "vitest";
import { api, me } from "./api";

describe("X-Exuvia-Origin", () => {
  afterEach(() => vi.unstubAllGlobals());

  const capture = () => {
    const fetch = vi.fn(async () => new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetch);
    return () => (fetch.mock.calls.at(-1) as unknown as [string, RequestInit])[1].headers as Record<string, string>;
  };

  it("labels the project API's requests as the ui, so code saved from it says so", async () => {
    const headers = capture();
    await api.save("nums", "x = 1");
    expect(headers()["X-Exuvia-Origin"]).toBe("ui");
    await api.saveViewFile("board", "App.tsx", "export default () => null;");
    expect(headers()["X-Exuvia-Origin"]).toBe("ui");
  });

  it("sends none outside the project API", async () => {
    const headers = capture();
    await me();
    expect(headers()["X-Exuvia-Origin"]).toBeUndefined();
  });
});
