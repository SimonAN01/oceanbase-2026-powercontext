import assert from "node:assert/strict";
import { test } from "node:test";
import { reviewQueue } from "../src/queue.mjs";

test("without a cutoff the review queue keeps all active cards in input order", () => {
  const cards = [
    { id: "later", active: true, dueOn: "2026-11-08" },
    { id: "retired", active: false },
    { id: "earlier", active: true, dueOn: "2026-11-01" },
  ];
  assert.deepEqual(reviewQueue(cards), ["later", "earlier"]);
  assert.deepEqual(reviewQueue([]), []);
});
