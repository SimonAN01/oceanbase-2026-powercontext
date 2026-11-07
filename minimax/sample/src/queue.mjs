/** Return the ids of active cards in the original review queue order. */
export function reviewQueue(cards) {
  return cards.filter((card) => card.active).map((card) => card.id);
}
