import { describe, expect, it } from "vitest";
import { sumAmounts } from "./format";

describe("sumAmounts", () => {
  it("adds decimal-string amounts keyed by corporate action", () => {
    expect(sumAmounts({ "0050:DIVIDEND:2026-07-21": "600.0", "0050:DIVIDEND:2027-01-20": "80" })).toBe(680);
  });

  it("is zero when nothing is owed", () => {
    expect(sumAmounts({})).toBe(0);
  });
});
