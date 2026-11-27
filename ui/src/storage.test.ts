import { describe, expect, it } from "vitest";
import type { StorageState } from "./api";
import { files, places, ratio } from "./Storage";

describe("storage", () => {
  it("tells compression as how many times smaller, or nothing", () => {
    expect(ratio(3400, 1000)).toBe("3.4×");
    expect(ratio(25_000, 1000)).toBe("25×");
    expect(ratio(0, 1000)).toBe("");
    expect(ratio(1000, 0)).toBe("");
  });

  it("counts files in words", () => {
    expect(files(1)).toBe("1 file");
    expect(files(1200)).toBe(`${(1200).toLocaleString()} files`);
  });

  it("lists the project's own files beside .exuvia's parts, largest first", () => {
    const s = {
      code: { bytes: 50, files: 4 },
      state: [
        { key: "artifacts", label: "Node outputs", bytes: 900, files: 3 },
        { key: "exuvia.db", label: "Records database", bytes: 20, files: 1 },
      ],
    } as unknown as StorageState;
    expect(places(s).map((p) => p.key)).toEqual(["artifacts", "code", "exuvia.db"]);
    expect(places(s)[1].label).toBe("Project files");
  });
});
