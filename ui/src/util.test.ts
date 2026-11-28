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

  it("leaves maths out rather than show raw TeX (#253)", () => {
    // A formula on its own line ends the summary, as a code fence does.
    expect(summary("Drawdown per ticker: the decline from the peak close.\n\n$dd_t = \\frac{\\text{close}_t}{\\text{cummax}(\\text{close})_t} - 1$")).toBe(
      "Drawdown per ticker: the decline from the peak close.",
    );
    expect(summary("Drawdown per ticker.\n$$\ndd_t = x - 1\n$$\nMore.")).toBe("Drawdown per ticker.");
    expect(summary("Drawdown per ticker.\n$$dd_t = x - 1$$")).toBe("Drawdown per ticker.");
    // Inside a sentence it becomes "…".
    expect(summary("Drawdown from peak per ticker (%), where $\\text{drawdown}_t = x$.")).toBe("Drawdown from peak per ticker (%), where ….");    // A leading formula is skipped, like a leading fence's marks.
    expect(summary("$$\nx = 1\n$$\n\nThe ratio.")).toBe("The ratio.");
    expect(summary("$x = 1$\nThe ratio.")).toBe("The ratio.");
  });

  it("keeps dollars that are not maths", () => {
    expect(summary("Orders from $5 to $10")).toBe("Orders from $5 to $10");
    expect(summary("Filtered by $as_of")).toBe("Filtered by $as_of");
    expect(summary("Reads `$x$` as written")).toBe("Reads $x$ as written");
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
