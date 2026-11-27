import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { hubApi } from "./api";
import { noteAnswer, resetSession, signedOutNow } from "./session";

describe("session", () => {
  beforeEach(resetSession);
  afterEach(() => vi.unstubAllGlobals());

  it("a 401 before any answer is not a lapsed sign-in", () => {
    noteAnswer(401);
    expect(signedOutNow()).toBe(false);
  });

  it("a 401 after an answer is", () => {
    noteAnswer(200);
    noteAnswer(401);
    expect(signedOutNow()).toBe(true);
  });

  it("an answer again means signed in again", () => {
    noteAnswer(200);
    noteAnswer(401);
    noteAnswer(404);
    expect(signedOutNow()).toBe(true);
    noteAnswer(200);
    expect(signedOutNow()).toBe(false);
  });

  const answer = (status: number) =>
    vi.stubGlobal("fetch", vi.fn(async () => new Response('{"detail":"x"}', { status, headers: { "Content-Type": "application/json" } })));

  it("API calls note what they are answered", async () => {
    answer(200);
    await hubApi.projects();
    answer(401);
    await expect(hubApi.projects()).rejects.toThrow();
    expect(signedOutNow()).toBe(true);
  });

  it("a token being checked that is turned away is not a lapsed sign-in", async () => {
    answer(200);
    await hubApi.projects();
    answer(401);
    await expect(hubApi.projects("wrong")).rejects.toThrow();
    expect(signedOutNow()).toBe(false);
  });
});
