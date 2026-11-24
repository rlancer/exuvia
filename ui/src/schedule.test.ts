import { describe, expect, it } from "vitest";
import { fireTime, liveEvery, upcoming } from "./Schedule";

describe("schedule", () => {
  const mon18 = Date.UTC(2026, 0, 5, 18, 0) / 1000; // a Monday, 18:00 UTC

  it("shows a fire on the project's clock, with the date once it is a week out", () => {
    expect(fireTime(mon18, "UTC", mon18 - 3600)).toBe("Mon 18:00");
    expect(fireTime(mon18, "America/New_York", mon18 - 3600)).toBe("Mon 13:00");
    expect(fireTime(mon18, "UTC", mon18 - 10 * 86400)).toBe("Jan 5 18:00");
    expect(fireTime(mon18, "Not/AZone", mon18 - 3600)).toMatch(/^Mon \d\d:00$/); // the browser's own
  });

  it("merges every node's next fires, soonest first", () => {
    const row = (name: string, times: number[]) => ({ name, cron: "x", upcoming: times }) as never;
    expect(upcoming([row("a", [10, 30]), row("b", [5, 30])], 3)).toEqual([
      { at: 5, node: "b", cron: "x" },
      { at: 10, node: "a", cron: "x" },
      { at: 30, node: "a", cron: "x" },
    ]);
  });

  it("says how often a live node re-runs", () => {
    expect(liveEvery(0)).toBe("every open");
    expect(liveEvery(300)).toBe("when over 5m old");
    expect(liveEvery(7200)).toBe("when over 2h old");
    expect(liveEvery(45)).toBe("when over 45s old");
  });
});
