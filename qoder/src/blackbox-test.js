/**
 * Black-box test for Experiment Ledger with real seekdb.
 * Uses a temporary database directory.
 * Tests: seed, search, save, compare, restart persistence.
 */

import { SeekDB } from './seekdb.js';
import { checkComparability, calculateDifference, buildComparisonGroup } from './compare.js';
import { mkdtempSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { spawnSync } from 'node:child_process';

if (!process.env.LEDGER_TEST_DB) {
  const tempDir = mkdtempSync(join(tmpdir(), 'seekdb-test-'));
  try {
    const run = spawnSync(process.execPath, [process.argv[1]], {
      env: { ...process.env, LEDGER_TEST_DB: join(tempDir, '.seekdb') },
      stdio: 'inherit',
    });
    process.exitCode = run.status ?? 1;
  } finally {
    rmSync(tempDir, { recursive: true, force: true });
  }
}

const DB_PATH = process.env.LEDGER_TEST_DB;

let passed = 0;
let failed = 0;

async function assert(condition, message) {
  if (condition) {
    console.log(`  ✓ ${message}`);
    passed++;
  } else {
    console.error(`  ✗ ${message}`);
    failed++;
  }
}

async function testVersion() {
  console.log('\n[Test] seekdb version');
  const db = new SeekDB({ dbPath: DB_PATH, database: 'test' });
  await db.init();
  const version = await db.getVersion();
  console.log(`  seekdb version: ${version}`);
  await assert(typeof version === 'string' && version.length > 0, `Version string returned: ${version}`);
  return db;
}

async function testInsert(db) {
  console.log('\n[Test] Insert records');

  const r1 = await db.insert({
    name: 'Exp-A ResNet baseline',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'accuracy',
    value: 93.2,
    status: 'completed',
    config: 'ResNet-56, SGD lr=0.1, epochs=200',
    notes: '基线实验',
  });
  await assert(r1._id !== undefined, 'Insert returns record with _id');

  const count1 = await db.count();
  await assert(count1 === 1, `Count is 1 after first insert (got ${count1})`);

  await db.insert({
    name: 'Exp-B ResNet Cutout',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'accuracy',
    value: 94.7,
    status: 'completed',
    config: 'ResNet-56, SGD lr=0.1, epochs=200, Cutout',
    notes: '增加 Cutout 数据增强',
  });

  await db.insert({
    name: 'Exp-C ResNet 五折交叉验证',
    dataset: 'CIFAR-10',
    split: '5-fold CV',
    metric: 'accuracy',
    value: 92.8,
    status: 'completed',
    config: 'ResNet-56, 5-fold cross validation',
    notes: '不同划分',
  });

  await db.insert({
    name: 'Exp-D VGG running',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'accuracy',
    value: 95.0,
    status: 'running',
    config: 'VGG-16, training',
    notes: '进行中',
  });

  const count2 = await db.count();
  await assert(count2 === 4, `Count is 4 after four inserts (got ${count2})`);

  return r1._id;
}

async function testSearch(db) {
  console.log('\n[Test] Full-text keyword search');

  const resnet = await db.search('ResNet');
  await assert(resnet.length === 3, `Search "ResNet" finds 3 records (got ${resnet.length})`);

  const cutout = await db.search('Cutout');
  await assert(cutout.length >= 1, `Search "Cutout" finds at least 1 record (got ${cutout.length})`);

  const baseline = await db.search('baseline');
  await assert(baseline.length >= 1, `Search "baseline" finds at least 1 record (got ${baseline.length})`);

  const chinese = await db.search('基线');
  await assert(chinese.length >= 1, `Search "基线" (Chinese) finds at least 1 record (got ${chinese.length})`);

  const none = await db.search('nonexistentxyz');
  await assert(none.length === 0, `Search "nonexistentxyz" finds 0 (got ${none.length})`);

  const empty = await db.search('');
  await assert(empty.length === 4, `Empty search returns all 4 records (got ${empty.length})`);
}

async function testFilter(db) {
  console.log('\n[Test] Field filtering');

  const cifar = await db.filter({ dataset: 'CIFAR-10' });
  await assert(cifar.length === 4, `Filter by dataset CIFAR-10 finds 4 (got ${cifar.length})`);

  const split8020 = await db.filter({ split: 'train/test 80/20' });
  await assert(split8020.length === 3, `Filter by split finds 3 (got ${split8020.length})`);

  const combined = await db.filter({ dataset: 'CIFAR-10', split: 'train/test 80/20' });
  await assert(combined.length === 3, `Combined filter finds 3 (got ${combined.length})`);

  const completed = await db.filter({ status: 'completed' });
  await assert(completed.length === 3, `Filter completed finds 3 (got ${completed.length})`);

  const noMatch = await db.filter({ dataset: 'ImageNet' });
  await assert(noMatch.length === 0, `Filter for nonexistent dataset finds 0 (got ${noMatch.length})`);
}

async function testQuery(db) {
  console.log('\n[Test] Combined query (search + filter)');

  const q1 = await db.query({ keywords: 'ResNet', dataset: 'CIFAR-10', split: 'train/test 80/20' });
  await assert(q1.length === 2, `Query "ResNet" + CIFAR-10 + split finds 2 (got ${q1.length})`);

  const q2 = await db.query({ split: '5-fold CV' });
  await assert(q2.length === 1, `Query by split only finds 1 (got ${q2.length})`);

  const q3 = await db.query({ keywords: 'Cutout', status: 'completed' });
  await assert(q3.length >= 1, `Query "Cutout" + completed finds at least 1 (got ${q3.length})`);
}

async function testComparability() {
  console.log('\n[Test] Comparability checks');

  const a = { name: 'A', dataset: 'CIFAR-10', split: 'train/test 80/20', metric: 'accuracy', value: 93.2, status: 'completed' };
  const b = { name: 'B', dataset: 'CIFAR-10', split: 'train/test 80/20', metric: 'accuracy', value: 94.7, status: 'completed' };
  const c = { name: 'C', dataset: 'CIFAR-10', split: '5-fold CV', metric: 'accuracy', value: 92.8, status: 'completed' };
  const d = { name: 'D', dataset: 'CIFAR-10', split: 'train/test 80/20', metric: 'accuracy', value: 95.0, status: 'running' };

  const ab = checkComparability(a, b);
  await assert(ab.comparable === true, 'Same dataset+split+metric+completed → comparable');

  const ac = checkComparability(a, c);
  await assert(ac.comparable === false, 'Different split → not comparable');
  await assert(ac.reasons.some((r) => r.includes('划分不同')), 'Reason mentions split difference');

  const ad = checkComparability(a, d);
  await assert(ad.comparable === false, 'One running → not comparable');
  await assert(ad.reasons.some((r) => r.includes('running')), 'Reason mentions running status');
}

async function testDifference() {
  console.log('\n[Test] Difference calculation');

  const a = { metric: 'accuracy', value: 93.2 };
  const b = { metric: 'accuracy', value: 94.7 };
  const diff = calculateDifference(a, b);
  await assert(Math.abs(diff.absolute - 1.5) < 0.001, `Absolute diff is 1.5 (got ${diff.absolute})`);
  await assert(diff.direction === 'improved', 'Higher accuracy → improved');

  const x = { metric: 'test loss', value: 0.25 };
  const y = { metric: 'test loss', value: 0.185 };
  const diffLoss = calculateDifference(x, y);
  await assert(diffLoss.direction === 'improved', 'Lower loss → improved');
  await assert(diffLoss.absolute < 0, 'Loss diff is negative');
}

async function testComparableLookup(db, idA) {
  console.log('\n[Test] Find comparable records');

  const comparable = await db.findComparable(idA);
  await assert(comparable.length >= 1, `Found ${comparable.length} comparable record(s) for Exp-A`);
  await assert(
    comparable.every((r) => r.split === 'train/test 80/20' && r.status === 'completed'),
    'All comparable records have same split and completed status'
  );
}

async function testGroups(db) {
  console.log('\n[Test] Comparison groups');

  const all = await db.findAll();
  const groups = buildComparisonGroup(all);
  await assert(groups.length >= 1, `Found ${groups.length} comparison group(s)`);

  const mainGroup = groups.find(
    (g) => g.dataset === 'CIFAR-10' && g.split === 'train/test 80/20' && g.metric === 'accuracy'
  );
  await assert(mainGroup !== undefined, 'Found CIFAR-10 / train-test-80-20 / accuracy group');

  if (mainGroup) {
    const completedInGroup = mainGroup.records.filter((r) => r.status === 'completed');
    await assert(completedInGroup.length === 2, `Group has 2 completed records (got ${completedInGroup.length})`);
    await assert(mainGroup.comparable.length === 1, `Group has 1 comparable pair (got ${mainGroup.comparable.length})`);
  }
}

async function testPersistence() {
  console.log('\n[Test] Persistence (simulate restart)');

  // Create new instance pointing to same database
  const db2 = new SeekDB({ dbPath: DB_PATH, database: 'test' });
  await db2.init();

  const count = await db2.count();
  await assert(count === 4, `DB2 (restart) has 4 records (got ${count})`);

  const found = await db2.search('ResNet');
  await assert(found.length === 3, `Search finds 3 ResNet records after restart (got ${found.length})`);

  const version = await db2.getVersion();
  await assert(typeof version === 'string' && version.length > 0, `Version after restart: ${version}`);
}

async function testFindById(db, id) {
  console.log('\n[Test] Find by ID');

  const record = await db.findById(id);
  await assert(record !== null, 'Found record by ID');
  await assert(record.name === 'Exp-A ResNet baseline', 'Record name matches');
  await assert(record.value === 93.2, 'Record value matches');

  const notFound = await db.findById('nonexistent-id');
  await assert(notFound === null, 'Returns null for nonexistent ID');
}

async function testStats(db) {
  console.log('\n[Test] Stats');

  const stats = await db.getStats();
  await assert(stats.total === 4, `Total is 4 (got ${stats.total})`);
  await assert(stats.datasets.includes('CIFAR-10'), 'Datasets includes CIFAR-10');
  await assert(stats.splits.includes('train/test 80/20'), 'Splits includes train/test 80/20');
  await assert(stats.metrics.includes('accuracy'), 'Metrics includes accuracy');
}

// Run all tests
async function main() {
  console.log('=== Experiment Ledger Black-Box Test Suite ===');
  const db = await testVersion();
  const idA = await testInsert(db);
  await testSearch(db);
  await testFilter(db);
  await testQuery(db);
  await testComparability();
  await testDifference();
  await testComparableLookup(db, idA);
  await testGroups(db);
  await testFindById(db, idA);
  await testStats(db);
  await testPersistence();

  console.log(`\n=== Results: ${passed} passed, ${failed} failed ===`);
  return failed === 0 ? 0 : 1;
}

if (DB_PATH) {
  main().then((status) => process.exit(status), (err) => {
    console.error('Test failed:', err);
    process.exit(1);
  });
}
