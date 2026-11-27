import { describe, expect, it } from "vitest";
import { failedShare, usageLine } from "./AdminKeys";

const u = (requests: number, queries = 0, errors = 0) => ({ requests, queries, errors });

describe("api key usage", () => {
  it("says a key's use in words", () => {
    expect(usageLine(u(0))).toBe("not used");
    expect(usageLine(u(1))).toBe("1 request");
    expect(usageLine(u(12, 1, 3))).toBe("12 requests, 1 query, 3 failed");
  });

  it("tells the failed share, and nothing when none failed", () => {
    expect(failedShare(u(10, 0, 0))).toBe("");
    expect(failedShare(u(200, 0, 1))).toBe("<1%");
    expect(failedShare(u(8, 0, 2))).toBe("25%");
  });
});
