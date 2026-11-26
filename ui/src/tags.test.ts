import { describe, expect, it } from "vitest";
import { tagNameProblem } from "./tags";

describe("tagNameProblem", () => {
  it("takes the names git takes", () => {
    for (const ok of ["v1", "v1.2.0", "q3-review", "before_refactor", "A", "x_"]) expect(tagNameProblem(ok)).toBeNull();
  });
  it("explains the rest", () => {
    for (const bad of ["", " ", "-x", ".x", "a b", "x/y", "a..b", "v1.lock", "end.", "end-", "x".repeat(65)])
      expect(tagNameProblem(bad)).not.toBeNull();
  });
});
