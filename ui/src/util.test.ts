import { describe, expect, it } from "vitest";
import { fmtBytes, summary } from "./util";

describe("summary", () => {
  it("is the first paragraph, on one line", () => {
    expect(summary("Daily closes per ticker,\nfrom the vendor.\n\nMore detail here.")).toBe("Daily closes per ticker, from the vendor.");
  });

  it("takes the marks off and keeps the words", () => {
    expect(summary("**20-day** _annualized_ vol from `returns`, see [the memo](https://x.test/m).")).toBe(
      "20-day annualized vol from returns, see the memo.",
    );
    expect(summary("# Realized vol")).toBe("Realized vol");
    expect(summary("- one\n- two")).toBe("one two");
  });

  it("leaves underscores and stars inside names and code alone", () => {
    expect(summary("Joins rolling_vol_20d to universe")).toBe("Joins rolling_vol_20d to universe");
    expect(summary("Reads `a_*_b` and 2*3*4")).toBe("Reads a_*_b and 2*3*4");
  });

  it("skips a leading fence and blank lines", () => {
    expect(summary("\n\n```\ncode\n```")).toBe("code");
    expect(summary("")).toBe("");
  });
});

describe("fmtBytes", () => {
  it("keeps to the largest unit at 1 or more", () => {
    expect(fmtBytes(812)).toBe("812 B");
    expect(fmtBytes(1536)).toBe("1.5 KB");
    expect(fmtBytes(42 * 1024 * 1024)).toBe("42 MB");
    expect(fmtBytes(null)).toBe("");
  });
});
