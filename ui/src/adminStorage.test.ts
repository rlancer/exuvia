import { describe, expect, it } from "vitest";
import { diskLow, hubPlaces } from "./AdminStorage";

describe("hub storage", () => {
  it("lists the projects' kinds of space beside the shared folders, largest first, empty ones left out", () => {
    const places = hubPlaces({
      kinds: {
        outputs: { bytes: 900, files: 3 },
        code: { bytes: 50, files: 4 },
        database: { bytes: 300, files: 2 },
        caches: { bytes: 0, files: 0 },
        other: { bytes: 10, files: 1 },
      },
      shared: [
        { key: "packages", label: "Python packages (shared)", bytes: 500, files: 40 },
        { key: "other", label: "Other", bytes: 1, files: 1 },
      ],
    });
    expect(places.map((p) => p.key)).toEqual(["outputs", "shared:packages", "database", "code", "other", "shared:other"]);
    expect(places[1].shared).toBe(true);
    expect(places.at(-1)?.label).toBe("Other in the hub's folder");
  });

  it("calls the disk low under a tenth free", () => {
    expect(diskLow({ total: 100, used: 95, free: 5 })).toBe(true);
    expect(diskLow({ total: 100, used: 50, free: 50 })).toBe(false);
    expect(diskLow(null)).toBe(false);
  });
});
