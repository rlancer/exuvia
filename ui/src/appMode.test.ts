import { describe, expect, it } from "vitest";
import type { ProjectParam } from "./api";
import { appSearch, appUrl, appViewOf, linkToApp, paramsOf } from "./appMode";

const specs: ProjectParam[] = [
  { name: "as_of", type: "date", default: null, value: null },
  { name: "top", type: "number", default: 10, value: 10 },
  { name: "live", type: "boolean", default: false, value: false },
  { name: "region", type: "string", default: "", value: "" },
];

describe("app mode addresses", () => {
  it("reads the view only when it is a view name", () => {
    expect(appViewOf("?app=vol_board")).toBe("vol_board");
    expect(appViewOf("?embed&app=vol_board")).toBe("vol_board");
    expect(appViewOf("")).toBeNull();
    expect(appViewOf("?app=")).toBeNull();
    expect(appViewOf("?app=../x")).toBeNull();
  });

  it("round-trips the parameters, typed, and drops what it doesn't know", () => {
    const params = { top: 5, live: true, as_of: "2024-03-01", region: "EU & UK" };
    const search = appSearch("vol_board", params);
    expect(search).toBe("?app=vol_board&p.as_of=2024-03-01&p.live=true&p.region=EU+%26+UK&p.top=5");
    expect(paramsOf(search, specs)).toEqual(params);
    expect(paramsOf("?app=v&p.top=lots&p.live=yes&p.nope=1", specs)).toEqual({});
    expect(appSearch("v", { top: null, region: "" })).toBe("?app=v");
  });

  it("builds the address on a hub, on a server of its own, and through a share link", () => {
    expect(appUrl("https://hub.example", "sales", "board")).toBe("https://hub.example/p/sales/?app=board");
    expect(appUrl("http://127.0.0.1:7420", null, "board", { top: 3 })).toBe("http://127.0.0.1:7420/?app=board&p.top=3");
    expect(linkToApp("https://hub.example/s/exs_abc", "board")).toBe("https://hub.example/s/exs_abc?app=board");
    expect(linkToApp("https://hub.example/s/exs_abc", null)).toBe("https://hub.example/s/exs_abc");
  });
});
