# REVIEW-17 · Due review queue

Add an optional second argument `asOf` to `reviewQueue(cards)`, exported by `src/queue.mjs`.

- With `asOf` omitted or `undefined`, include all active cards and preserve their input order.
- With a valid `asOf` date string, include only active cards with `dueOn <= asOf`, including cards due exactly on that date. Preserve the input order.
- Dates use valid Gregorian calendar days in exactly `YYYY-MM-DD` format, between years 0001 and 9999. Compare calendar days, regardless of timezone.
- A supplied invalid date cutoff throws `RangeError`, even when the deck is empty. With a cutoff, exclude active cards with missing or invalid `dueOn` dates.
- Do not mutate the input or its cards. Keep the returned array of card IDs.
