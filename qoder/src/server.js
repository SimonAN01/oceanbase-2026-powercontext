import { createServer } from 'node:http';
import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { SeekDB } from './seekdb.js';
import { checkComparability, calculateDifference, buildComparisonGroup } from './compare.js';

const __dirname = dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = join(__dirname, '..', 'public');

let db;

async function initDB() {
  db = new SeekDB();
  await db.init();
  return db;
}

function parseBody(req) {
  return new Promise((resolve, reject) => {
    let body = '';
    req.on('data', (chunk) => (body += chunk));
    req.on('end', () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch (e) {
        reject(e);
      }
    });
    req.on('error', reject);
  });
}

function json(res, data, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
  });
  res.end(JSON.stringify(data));
}

function serveStatic(res, filePath) {
  const extMap = {
    '.html': 'text/html; charset=utf-8',
    '.css': 'text/css; charset=utf-8',
    '.js': 'application/javascript; charset=utf-8',
    '.json': 'application/json; charset=utf-8',
    '.png': 'image/png',
    '.svg': 'image/svg+xml',
  };
  const ext = filePath.match(/\.\w+$/)?.[0] || '';
  const contentType = extMap[ext] || 'application/octet-stream';

  if (!existsSync(filePath)) {
    res.writeHead(404);
    res.end('Not found');
    return;
  }

  const content = readFileSync(filePath);
  res.writeHead(200, { 'Content-Type': contentType });
  res.end(content);
}

async function handleAPI(req, res, url) {
  const method = req.method;

  if (method === 'OPTIONS') {
    return json(res, {});
  }

  // GET /api/version
  if (url.pathname === '/api/version' && method === 'GET') {
    const version = await db.getVersion();
    return json(res, { version });
  }

  // GET /api/experiments
  if (url.pathname === '/api/experiments' && method === 'GET') {
    const params = url.searchParams;
    const results = await db.query({
      keywords: params.get('q') || '',
      dataset: params.get('dataset') || '',
      split: params.get('split') || '',
      metric: params.get('metric') || '',
      status: params.get('status') || '',
    });
    return json(res, { results, total: results.length });
  }

  // POST /api/experiments
  if (url.pathname === '/api/experiments' && method === 'POST') {
    if (req.headers['content-type']?.split(';')[0] !== 'application/json') {
      return json(res, { error: 'Expected application/json' }, 415);
    }
    const body = await parseBody(req).catch(() => null);
    const required = ['name', 'dataset', 'split', 'metric'];
    for (const field of required) {
      if (typeof body?.[field] !== 'string' || !body[field].trim() || body[field].length > 255) {
        return json(res, { error: `Invalid field: ${field}` }, 400);
      }
    }
    if (!Number.isFinite(body.value)) return json(res, { error: 'Invalid metric value' }, 400);
    const record = await db.insert({
      name: body.name,
      dataset: body.dataset,
      split: body.split,
      metric: body.metric,
      value: body.value,
      status: body.status || 'completed',
      config: body.config || '',
      notes: body.notes || '',
    });
    return json(res, record, 201);
  }

  // GET /api/experiments/:id
  const matchId = url.pathname.match(/^\/api\/experiments\/([^/]+)$/);
  if (matchId && method === 'GET') {
    const record = await db.findById(matchId[1]);
    if (!record) return json(res, { error: 'Not found' }, 404);
    return json(res, record);
  }

  // GET /api/compare?id=x&id=y
  if (url.pathname === '/api/compare' && method === 'GET') {
    const ids = url.searchParams.getAll('id');
    if (ids.length < 2) {
      return json(res, { error: 'Need at least 2 experiment IDs' }, 400);
    }
    const records = [];
    for (const id of ids) {
      const r = await db.findById(id);
      if (r) records.push(r);
    }
    if (records.length < 2) {
      return json(res, { error: 'Could not find enough records' }, 404);
    }

    const pairs = [];
    for (let i = 0; i < records.length; i++) {
      for (let j = i + 1; j < records.length; j++) {
        const comp = checkComparability(records[i], records[j]);
        pairs.push({
          a: records[i],
          b: records[j],
          comparable: comp.comparable,
          reasons: comp.reasons,
          difference: comp.comparable ? calculateDifference(records[i], records[j]) : null,
        });
      }
    }
    return json(res, { records, pairs });
  }

  // GET /api/comparable/:id
  const matchComparable = url.pathname.match(/^\/api\/comparable\/([^/]+)$/);
  if (matchComparable && method === 'GET') {
    const record = await db.findById(matchComparable[1]);
    if (!record) return json(res, { error: 'Not found' }, 404);
    const comparable = await db.findComparable(matchComparable[1]);
    const comparisons = comparable.map((other) => ({
      record: other,
      comparable: true,
      difference: calculateDifference(record, other),
    }));
    return json(res, { record, comparisons });
  }

  // GET /api/groups
  if (url.pathname === '/api/groups' && method === 'GET') {
    const all = await db.findAll();
    const groups = buildComparisonGroup(all);
    return json(res, { groups });
  }

  // GET /api/stats
  if (url.pathname === '/api/stats' && method === 'GET') {
    const stats = await db.getStats();
    return json(res, stats);
  }

  return json(res, { error: 'Not found' }, 404);
}

const PORT = process.env.PORT || 3000;

async function start() {
  await initDB();
  const count = await db.count();
  const version = await db.getVersion();

  const server = createServer(async (req, res) => {
    const url = new URL(req.url, `http://localhost:${PORT}`);

    if (url.pathname.startsWith('/api/')) {
      try {
        await handleAPI(req, res, url);
      } catch (err) {
        console.error('API error:', err);
        json(res, { error: err.message }, 500);
      }
      return;
    }

    const filePath = url.pathname === '/' ? '/index.html' : url.pathname;
    if (['/index.html', '/style.css', '/app.js'].includes(filePath)) {
      return serveStatic(res, join(PUBLIC_DIR, filePath));
    }
    res.writeHead(404).end();
  });

  server.listen(PORT, '127.0.0.1', () => {
    console.log(`Experiment Ledger running at http://localhost:${PORT}`);
    console.log(`seekdb version: ${version}`);
    console.log(`Records: ${count}`);
  });
}

start().catch((err) => {
  console.error('Failed to start server:', err);
  process.exit(1);
});
