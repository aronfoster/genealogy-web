import test from "node:test";
import assert from "node:assert/strict";
import { matchesRender } from "../scripts/verify_prototype.mjs";

test("only the exact released source and reviewed capture may use the same-host pixel comparison", () => {
  const file = "screen.png";
  const manifest = (sourceHash, digest) => ({
    sourceHash,
    captures: { [file]: digest },
  });
  const proposed = manifest("source", "reviewed");
  const reference = {
    committed: manifest("source", "reviewed"),
    rendered: manifest("source", "host-specific"),
  };
  assert(matchesRender("reviewed", file, proposed, null));
  assert(matchesRender("host-specific", file, proposed, reference));
  assert(!matchesRender("different-pixels", file, proposed, reference));
  assert(
    !matchesRender(
      "host-specific",
      file,
      manifest("source", "new-golden"),
      reference,
    ),
  );
  assert(
    !matchesRender(
      "host-specific",
      file,
      manifest("changed", "reviewed"),
      reference,
    ),
  );
  assert(
    !matchesRender("host-specific", file, proposed, {
      ...reference,
      rendered: manifest("changed", "host-specific"),
    }),
  );
});
