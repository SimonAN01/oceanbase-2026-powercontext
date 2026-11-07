/**
 * Experiment Ledger — Frontend Application
 * Handles search, filter, add, compare, and group views.
 */

const API = '';

// State
let allExperiments = [];
let selectedForCompare = new Set();

// --- Tab Navigation ---
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById(`tab-${tab.dataset.tab}`).classList.add('active');

    if (tab.dataset.tab === 'compare') loadCompareSelect();
    if (tab.dataset.tab === 'groups') loadGroups();
  });
});

// --- API Helpers ---
async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  return res.json();
}

// --- Load Stats & Populate Filters ---
async function loadStats() {
  const stats = await api('/api/stats');
  const statsBar = document.getElementById('stats-bar');
  statsBar.textContent = '';
  statsBar.appendChild(makeSpan(`共 ${stats.total} 条实验记录`));
  statsBar.appendChild(makeSpan(`数据集: ${stats.datasets.join(', ') || '-'}`));
  statsBar.appendChild(makeSpan(`划分: ${stats.splits.join(', ') || '-'}`));
  statsBar.appendChild(makeSpan(`指标: ${stats.metrics.join(', ') || '-'}`));

  populateSelect('filter-dataset', stats.datasets);
  populateSelect('filter-split', stats.splits);
  populateSelect('filter-metric', stats.metrics);
}

function makeSpan(text) {
  const span = document.createElement('span');
  span.textContent = text;
  return span;
}

function populateSelect(id, values) {
  const sel = document.getElementById(id);
  const current = sel.value;
  sel.innerHTML = '';
  const defaultOpt = document.createElement('option');
  defaultOpt.value = '';
  defaultOpt.textContent = sel.querySelector('option')?.textContent || '全部';
  sel.appendChild(defaultOpt);
  for (const v of values) {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  }
  sel.value = current;
}

// --- Search & Filter ---
async function loadExperiments() {
  const q = document.getElementById('search-input').value;
  const dataset = document.getElementById('filter-dataset').value;
  const split = document.getElementById('filter-split').value;
  const metric = document.getElementById('filter-metric').value;

  const params = new URLSearchParams();
  if (q) params.set('q', q);
  if (dataset) params.set('dataset', dataset);
  if (split) params.set('split', split);
  if (metric) params.set('metric', metric);

  const data = await api(`/api/experiments?${params}`);
  allExperiments = data.results;
  renderResults(data.results);
}

function renderResults(results) {
  const container = document.getElementById('results');
  if (results.length === 0) {
    container.textContent = '没有找到匹配的实验记录';
    container.style.cssText = 'padding:2rem;text-align:center;color:#94a3b8;';
    return;
  }

  const table = document.createElement('table');
  const thead = document.createElement('thead');
  thead.innerHTML = `
    <tr>
      <th><input type="checkbox" class="compare-check" id="select-all" title="全选"></th>
      <th>实验名称</th>
      <th>数据集</th>
      <th>划分</th>
      <th>指标</th>
      <th>数值</th>
      <th>状态</th>
      <th>配置</th>
    </tr>
  `;
  table.appendChild(thead);

  const tbody = document.createElement('tbody');
  for (const r of results) {
    const tr = document.createElement('tr');

    const tdCheck = document.createElement('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'compare-check';
    cb.value = r._id;
    cb.checked = selectedForCompare.has(r._id);
    tdCheck.appendChild(cb);
    tr.appendChild(tdCheck);

    const tdName = document.createElement('td');
    tdName.innerHTML = `<strong>${esc(r.name)}</strong><br><small style="color:#94a3b8">${esc(r.notes || '')}</small>`;
    tr.appendChild(tdName);

    tr.appendChild(makeTd(esc(r.dataset)));
    tr.appendChild(makeTd(esc(r.split)));
    tr.appendChild(makeTd(esc(r.metric)));

    const tdVal = document.createElement('td');
    tdVal.innerHTML = `<strong>${esc(String(r.value))}</strong>`;
    tr.appendChild(tdVal);

    const tdStatus = document.createElement('td');
    const badge = document.createElement('span');
    badge.className = `status-badge status-${validStatus(r.status)}`;
    badge.textContent = statusLabel(r.status);
    tdStatus.appendChild(badge);
    tr.appendChild(tdStatus);

    const tdConfig = document.createElement('td');
    tdConfig.className = 'config-cell';
    tdConfig.title = r.config;
    tdConfig.textContent = r.config;
    tr.appendChild(tdConfig);

    tbody.appendChild(tr);
  }
  table.appendChild(tbody);

  container.innerHTML = '';
  container.appendChild(table);

  // Checkbox handlers
  document.getElementById('select-all')?.addEventListener('change', (e) => {
    document.querySelectorAll('.compare-check:not(#select-all)').forEach((cb) => {
      cb.checked = e.target.checked;
      if (e.target.checked) {
        selectedForCompare.add(cb.value);
      } else {
        selectedForCompare.delete(cb.value);
      }
    });
  });

  container.querySelectorAll('.compare-check:not(#select-all)').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.checked) {
        selectedForCompare.add(cb.value);
      } else {
        selectedForCompare.delete(cb.value);
      }
    });
  });
}

function makeTd(html) {
  const td = document.createElement('td');
  td.innerHTML = html;
  return td;
}

function esc(str) {
  const div = document.createElement('div');
  div.textContent = String(str);
  return div.innerHTML;
}

function escAttr(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function validStatus(s) {
  const allowed = ['completed', 'running', 'failed', 'planned'];
  return allowed.includes(s) ? s : 'planned';
}

function validDirection(d) {
  const allowed = ['improved', 'regressed', 'unchanged'];
  return allowed.includes(d) ? d : 'unchanged';
}

function statusLabel(s) {
  return { completed: '已完成', running: '进行中', failed: '失败', planned: '计划中' }[s] || s;
}

// --- Search Events ---
document.getElementById('search-btn').addEventListener('click', loadExperiments);
document.getElementById('reset-btn').addEventListener('click', () => {
  document.getElementById('search-input').value = '';
  document.getElementById('filter-dataset').value = '';
  document.getElementById('filter-split').value = '';
  document.getElementById('filter-metric').value = '';
  loadExperiments();
});
document.getElementById('search-input').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') loadExperiments();
});

// --- Add Experiment ---
document.getElementById('add-form').addEventListener('submit', async (e) => {
  e.preventDefault();
  const form = e.target;
  const data = Object.fromEntries(new FormData(form));
  data.value = Number(data.value);

  const result = await api('/api/experiments', {
    method: 'POST',
    body: JSON.stringify(data),
  });

  const resultDiv = document.getElementById('add-result');
  if (result._id) {
    resultDiv.className = 'form-result success';
    resultDiv.textContent = `已添加实验: ${result.name} (ID: ${result._id.slice(0, 8)}...)`;
    form.reset();
    loadStats();
    loadExperiments();
  } else {
    resultDiv.className = 'form-result error';
    resultDiv.textContent = `添加失败: ${result.error}`;
  }
});

// --- Compare Tab ---
async function loadCompareSelect() {
  const data = await api('/api/experiments');
  const container = document.getElementById('compare-select');
  container.innerHTML = '';

  for (const r of data.results) {
    const label = document.createElement('label');

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.className = 'compare-select-check';
    cb.value = r._id;
    cb.checked = selectedForCompare.has(r._id);
    label.appendChild(cb);

    const nameSpan = document.createElement('span');
    nameSpan.innerHTML = `<strong>${esc(r.name)}</strong>`;
    label.appendChild(nameSpan);

    const infoSpan = document.createElement('span');
    infoSpan.style.color = '#64748b';
    infoSpan.textContent = `${r.dataset} / ${r.split} / ${r.metric} = ${r.value}`;
    label.appendChild(infoSpan);

    const badge = document.createElement('span');
    badge.className = `status-badge status-${validStatus(r.status)}`;
    badge.textContent = statusLabel(r.status);
    label.appendChild(badge);

    container.appendChild(label);
  }

  container.querySelectorAll('.compare-select-check').forEach((cb) => {
    cb.addEventListener('change', () => {
      if (cb.checked) {
        selectedForCompare.add(cb.value);
      } else {
        selectedForCompare.delete(cb.value);
      }
      updateCompareButton();
    });
  });

  updateCompareButton();
}

function updateCompareButton() {
  const btn = document.getElementById('compare-btn');
  btn.disabled = selectedForCompare.size < 2;
  btn.textContent = selectedForCompare.size >= 2
    ? `比较选中记录 (${selectedForCompare.size} 条)`
    : `至少选择 2 条记录 (已选 ${selectedForCompare.size} 条)`;
}

document.getElementById('compare-btn').addEventListener('click', async () => {
  const ids = [...selectedForCompare];
  if (ids.length < 2) return;

  const params = ids.map((id) => `id=${id}`).join('&');
  const data = await api(`/api/compare?${params}`);
  renderCompareResult(data);
});

function renderCompareResult({ records, pairs }) {
  const container = document.getElementById('compare-result');
  container.innerHTML = '';

  // Records overview card
  const overviewCard = document.createElement('div');
  overviewCard.className = 'compare-card';

  const overviewHeader = document.createElement('div');
  overviewHeader.className = 'compare-card-header';
  overviewHeader.textContent = `选中记录概览 (${records.length} 条)`;
  overviewCard.appendChild(overviewHeader);

  const overviewBody = document.createElement('div');
  overviewBody.className = 'compare-card-body';

  const table = document.createElement('table');
  table.style.cssText = 'width:100%;border-collapse:collapse;';
  table.innerHTML = `
    <tr style="border-bottom:1px solid #e2e8f0;">
      <th style="text-align:left;padding:0.5rem;color:#64748b;">名称</th>
      <th style="text-align:left;padding:0.5rem;color:#64748b;">数据集</th>
      <th style="text-align:left;padding:0.5rem;color:#64748b;">划分</th>
      <th style="text-align:left;padding:0.5rem;color:#64748b;">指标</th>
      <th style="text-align:left;padding:0.5rem;color:#64748b;">数值</th>
      <th style="text-align:left;padding:0.5rem;color:#64748b;">状态</th>
    </tr>
  `;

  for (const r of records) {
    const tr = document.createElement('tr');
    tr.style.borderBottom = '1px solid #f1f5f9';
    tr.innerHTML = `
      <td style="padding:0.5rem;"><strong>${esc(r.name)}</strong></td>
      <td style="padding:0.5rem;">${esc(r.dataset)}</td>
      <td style="padding:0.5rem;">${esc(r.split)}</td>
      <td style="padding:0.5rem;">${esc(r.metric)}</td>
      <td style="padding:0.5rem;"><strong>${esc(String(r.value))}</strong></td>
      <td style="padding:0.5rem;"><span class="status-badge status-${validStatus(r.status)}">${esc(statusLabel(r.status))}</span></td>
    `;
    table.appendChild(tr);
  }

  overviewBody.appendChild(table);
  overviewCard.appendChild(overviewBody);
  container.appendChild(overviewCard);

  // Pairs
  for (const pair of pairs) {
    const pairCard = document.createElement('div');
    pairCard.className = 'compare-card';

    const pairHeader = document.createElement('div');
    pairHeader.className = 'compare-card-header';
    pairHeader.textContent = `${pair.a.name} vs ${pair.b.name}`;
    pairCard.appendChild(pairHeader);

    const pairBody = document.createElement('div');
    pairBody.className = 'compare-card-body';

    if (pair.comparable) {
      const dirLabel = { improved: '提升', regressed: '下降', unchanged: '持平' };
      const dirClass = `diff-${validDirection(pair.difference.direction)}`;

      const pairDiv = document.createElement('div');
      pairDiv.className = 'compare-pair';
      pairDiv.innerHTML = `
        <div class="compare-item">
          <h4>${esc(pair.a.name)}</h4>
          <p>数值: <strong>${esc(String(pair.a.value))}</strong></p>
          <p>配置: ${esc(pair.a.config)}</p>
        </div>
        <div class="compare-arrow">
          <div class="diff-value ${dirClass}">${pair.difference.absolute > 0 ? '+' : ''}${esc(pair.difference.absolute.toFixed(4))}</div>
          <div class="${dirClass}">${esc(dirLabel[pair.difference.direction])} ${pair.difference.percentage !== null ? `(${pair.difference.percentage > 0 ? '+' : ''}${esc(String(pair.difference.percentage))}%)` : ''}</div>
        </div>
        <div class="compare-item">
          <h4>${esc(pair.b.name)}</h4>
          <p>数值: <strong>${esc(String(pair.b.value))}</strong></p>
          <p>配置: ${esc(pair.b.config)}</p>
        </div>
      `;
      pairBody.appendChild(pairDiv);
    } else {
      const notComp = document.createElement('div');
      notComp.className = 'not-comparable';
      notComp.innerHTML = `
        <h4>不可比较</h4>
        <ul>${pair.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>
      `;
      pairBody.appendChild(notComp);
    }

    pairCard.appendChild(pairBody);
    container.appendChild(pairCard);
  }
}

// --- Groups Tab ---
async function loadGroups() {
  const data = await api('/api/groups');
  const container = document.getElementById('groups-container');
  container.innerHTML = '';

  if (data.groups.length === 0) {
    container.textContent = '暂无可比较的实验组';
    container.style.cssText = 'padding:2rem;text-align:center;color:#94a3b8;';
    return;
  }

  for (const g of data.groups) {
    const dirLabel = { improved: '提升', regressed: '下降', unchanged: '持平' };

    const card = document.createElement('div');
    card.className = 'group-card';

    const header = document.createElement('div');
    header.className = 'group-header';
    header.innerHTML = `<h3>${esc(g.dataset)} / ${esc(g.split)} / ${esc(g.metric)}</h3>`;
    const badge = document.createElement('span');
    badge.className = 'badge';
    badge.textContent = `${g.records.length} 条记录 · ${g.comparable.length} 组对照`;
    header.appendChild(badge);
    card.appendChild(header);

    const body = document.createElement('div');
    body.className = 'group-body';

    const table = document.createElement('table');
    table.innerHTML = `
      <thead>
        <tr>
          <th>实验名称</th>
          <th>数值</th>
          <th>状态</th>
          <th>配置</th>
          <th>备注</th>
        </tr>
      </thead>
    `;
    const tbody = document.createElement('tbody');

    for (const r of g.records) {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>${esc(r.name)}</strong></td>
        <td>${esc(String(r.value))}</td>
        <td><span class="status-badge status-${validStatus(r.status)}">${esc(statusLabel(r.status))}</span></td>
        <td class="config-cell" title="${escAttr(r.config)}">${esc(r.config)}</td>
        <td>${esc(r.notes || '')}</td>
      `;
      tbody.appendChild(tr);
    }

    for (const c of g.comparable) {
      const tr = document.createElement('tr');
      tr.className = 'diff-row';
      const td = document.createElement('td');
      td.colSpan = 5;
      const diffSign = c.absolute > 0 ? '+' : '';
      const pctStr = c.percentage !== null ? `, ${c.percentage > 0 ? '+' : ''}${c.percentage}%` : '';
      td.innerHTML = `差值: ${esc(c.a.name)} → ${esc(c.b.name)}: <strong class="diff-${validDirection(c.direction)}">${esc(diffSign)}${esc(c.absolute.toFixed(4))}</strong> (${esc(dirLabel[c.direction])}${esc(pctStr)})`;
      tr.appendChild(td);
      tbody.appendChild(tr);
    }

    table.appendChild(tbody);
    body.appendChild(table);

    if (g.notComparable.length > 0) {
      const notComp = document.createElement('div');
      notComp.className = 'not-comparable';
      notComp.innerHTML = `
        <h4>以下记录不可比较</h4>
        <ul>${g.notComparable.map((nc) => `<li>${esc(nc.record.name)}: ${esc(nc.reason)}</li>`).join('')}</ul>
      `;
      body.appendChild(notComp);
    }

    card.appendChild(body);
    container.appendChild(card);
  }
}

// --- Init ---
loadStats();
loadExperiments();
