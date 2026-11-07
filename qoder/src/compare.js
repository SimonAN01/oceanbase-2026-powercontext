/**
 * Comparability logic for experiment records.
 * Two records are comparable when they share the same dataset, split, and metric,
 * AND both have a "completed" status.
 */

export function checkComparability(a, b) {
  const reasons = [];

  if (a.dataset !== b.dataset) {
    reasons.push(`数据集不同: "${a.dataset}" vs "${b.dataset}"`);
  }
  if (a.split !== b.split) {
    reasons.push(`划分不同: "${a.split}" vs "${b.split}"`);
  }
  if (a.metric !== b.metric) {
    reasons.push(`指标不同: "${a.metric}" vs "${b.metric}"`);
  }
  if (a.status !== 'completed') {
    reasons.push(`实验 "${a.name}" 状态为 "${a.status}"，非已完成`);
  }
  if (b.status !== 'completed') {
    reasons.push(`实验 "${b.name}" 状态为 "${b.status}"，非已完成`);
  }

  return {
    comparable: reasons.length === 0,
    reasons,
  };
}

export function calculateDifference(a, b) {
  const diff = b.value - a.value;
  const pct = a.value !== 0 ? (diff / Math.abs(a.value)) * 100 : null;

  // For accuracy-like metrics (higher is better), positive diff means b improved
  // For loss-like metrics (lower is better), negative diff means b improved
  const metricLower = ['loss', 'error', 'mse', 'mae', 'rmse', 'latency', 'time'].some(
    (kw) => a.metric.toLowerCase().includes(kw)
  );

  return {
    absolute: diff,
    percentage: pct !== null ? Math.round(pct * 100) / 100 : null,
    direction: metricLower
      ? diff < 0
        ? 'improved'
        : diff > 0
          ? 'regressed'
          : 'unchanged'
      : diff > 0
        ? 'improved'
        : diff < 0
          ? 'regressed'
          : 'unchanged',
  };
}

export function buildComparisonGroup(records) {
  const groups = new Map();

  for (const r of records) {
    const key = `${r.dataset}||${r.split}||${r.metric}`;
    if (!groups.has(key)) {
      groups.set(key, {
        dataset: r.dataset,
        split: r.split,
        metric: r.metric,
        records: [],
      });
    }
    groups.get(key).records.push(r);
  }

  const result = [];
  for (const group of groups.values()) {
    if (group.records.length < 2) continue;

    const completed = group.records.filter((r) => r.status === 'completed');
    const incomplete = group.records.filter((r) => r.status !== 'completed');
    const pairs = [];

    for (let i = 0; i < completed.length; i++) {
      for (let j = i + 1; j < completed.length; j++) {
        pairs.push({
          a: completed[i],
          b: completed[j],
          ...calculateDifference(completed[i], completed[j]),
        });
      }
    }

    result.push({
      ...group,
      comparable: pairs,
      notComparable: incomplete.map((r) => ({
        record: r,
        reason: `状态为 "${r.status}"，无法与已完成实验比较`,
      })),
    });
  }

  return result;
}
