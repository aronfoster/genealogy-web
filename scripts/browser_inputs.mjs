import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { closeSync, fstatSync, readSync } from "node:fs";

// Python validates the closed document before sealing the inherited handle.
// Positional reads are independent of the producer's file offset.
export function browserInputs() {
  const fd = Number(process.env.BROWSER_INPUT_FD);
  assert(Number.isSafeInteger(fd) && fd >= 3, "Missing browser input handle");
  try {
    const { size, mode } = fstatSync(fd);
    assert(size > 0 && size <= 4 * 1024 * 1024 && (mode & 0o222) === 0);
    const raw = Buffer.alloc(size);
    assert.equal(readSync(fd, raw, 0, size, 0), size);
    assert.equal(
      createHash("sha256").update(raw).digest("hex"),
      process.env.BROWSER_INPUT_SHA256,
    );
    return JSON.parse(raw.toString("utf8"));
  } finally {
    closeSync(fd);
    delete process.env.BROWSER_INPUT_FD;
  }
}
