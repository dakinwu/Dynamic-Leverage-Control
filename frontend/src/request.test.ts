import { describe, expect, it } from "vitest";
import { latestOnly, responseError } from "./request";

describe("latestOnly", () => {
  it("lets only the most recently started request apply its result", () => {
    const gate = latestOnly();
    const slow = gate.begin();
    const fast = gate.begin();
    expect(gate.isCurrent(fast)).toBe(true);
    expect(gate.isCurrent(slow)).toBe(false);
  });
});

describe("responseError", () => {
  it("uses the server's detail when the body has one", async () => {
    const response = new Response(JSON.stringify({ detail: "execution_id x is already recorded" }), {
      status: 409,
      headers: { "Content-Type": "application/json" },
    });
    const error = await responseError(response, "成交回報失敗");
    expect(error.message).toBe("成交回報失敗：execution_id x is already recorded");
  });

  it("falls back to the status when the body is not JSON", async () => {
    const error = await responseError(new Response("bad gateway", { status: 502 }), "同步失敗");
    expect(error.message).toBe("同步失敗 (502)");
  });
});
