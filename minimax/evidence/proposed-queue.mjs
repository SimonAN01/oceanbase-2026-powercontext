export function reviewQueue(cards, asOf) {
  if (asOf === undefined) {
    return cards.filter(card => card.active).map(card => card.id);
  }

  if (!isValidDateString(asOf)) {
    throw new RangeError("Invalid cutoff date");
  }

  const cutoff = asOf;

  return cards
    .filter(card => {
      if (!card.active) return false;
      const due = card.dueOn;
      if (typeof due !== "string") return false;
      if (!isValidDateString(due)) return false;
      return due <= cutoff;
    })
    .map(card => card.id);
}

function isLeapYear(year) {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysInMonth(year, month) {
  const common = [0,31,28,31,30,31,30,31,31,30,31,30,31];
  if (month === 2 && isLeapYear(year)) return 29;
  return common[month];
}

function isValidDateString(str) {
  if (typeof str !== "string" || str.length !== 10) return false;
  if (!/^\d{4}-\d{2}-\d{2}$/.test(str)) return false;
  const y = parseInt(str.slice(0,4), 10);
  const m = parseInt(str.slice(5,7), 10);
  const d = parseInt(str.slice(8,10), 10);
  if (y < 1 || y > 9999) return false;
  if (m < 1 || m > 12) return false;
  if (d < 1 || d > daysInMonth(y, m)) return false;
  return true;
}
