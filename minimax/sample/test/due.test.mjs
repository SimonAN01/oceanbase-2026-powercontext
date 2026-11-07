import assert from "node:assert/strict";
import { test } from "node:test";
import { reviewQueue } from "../src/queue.mjs";

test("the due review cutoff includes the boundary and ignores retired and invalid cards", () => {
  const cards = Object.freeze([
    Object.freeze({ id: "boundary", active: true, dueOn: "2028-02-29" }),
    Object.freeze({ id: "future", active: true, dueOn: "2028-03-01" }),
    Object.freeze({ id: "overdue", active: true, dueOn: "2028-02-28" }),
    Object.freeze({ id: "invalid", active: true, dueOn: "2027-02-29" }),
    Object.freeze({ id: "missing", active: true }),
    Object.freeze({ id: "retired", active: false, dueOn: "2028-02-28" }),
  ]);
  assert.deepEqual(reviewQueue(cards, "2028-02-29"), ["boundary", "overdue"]);
  assert.deepEqual(reviewQueue(cards, undefined), ["boundary", "future", "overdue", "invalid", "missing"]);
  assert.throws(() => reviewQueue([], "2027-02-29"), RangeError);
  assert.throws(() => reviewQueue([], "0000-01-01"), RangeError);
  assert.throws(() => reviewQueue([], "2028-2-29"), RangeError);
});
