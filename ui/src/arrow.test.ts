import { describe, expect, it } from "vitest";
import { tableFromArrays, tableToIPC } from "apache-arrow";
import { ARROW_STREAM, arrowMeta, readArrow } from "./arrow";

/** An Arrow IPC stream of `batches` record batches, with exuvia's schema metadata. */
function ipcStream(batches: number): Uint8Array {
  const parts = Array.from({ length: batches }, (_, b) =>
    tableFromArrays({ i: Int32Array.from({ length: 1000 }, (_, k) => b * 1000 + k) }),
  );
  const table = parts[0].concat(...parts.slice(1));
  table.schema.metadata.set("exuvia", JSON.stringify({ columns: [{ name: "i", type: "INTEGER" }] }));
  return tableToIPC(table, "stream");
}

/** A response whose body arrives in `size`-byte pieces, cutting messages anywhere. */
function chunked(bytes: Uint8Array, size: number): Response {
  let at = 0;
  const body = new ReadableStream<Uint8Array>({
    pull(controller) {
      if (at >= bytes.length) return controller.close();
      controller.enqueue(bytes.slice(at, at + size));
      at += size;
    },
  });
  return new Response(body, { headers: { "Content-Type": ARROW_STREAM } });
}

describe("readArrow", () => {
  it("reads a stream that arrives in pieces, batch boundaries anywhere", async () => {
    const table = await readArrow(chunked(ipcStream(3), 333));
    expect(table.batches.length).toBe(3);
    expect(table.numRows).toBe(3000);
    expect(table.getChild("i")?.get(2999)).toBe(2999);
    expect(arrowMeta(table).columns).toEqual([{ name: "i", type: "INTEGER" }]);
  });

  it("reads a body that arrives in one piece", async () => {
    const table = await readArrow(chunked(ipcStream(1), Infinity));
    expect(table.numRows).toBe(1000);
  });

  it("fails on a stream cut short", async () => {
    const bytes = ipcStream(2);
    await expect(readArrow(chunked(bytes.slice(0, bytes.length - 500), 4096))).rejects.toThrow();
  });
});
