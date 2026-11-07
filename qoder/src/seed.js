/**
 * Seed the database with demo experiment records.
 * All data is fictional — for demonstration purposes only.
 *
 * Includes:
 * - 2 comparable records: same dataset (CIFAR-10) + same split (train/test 80/20) + same metric (accuracy)
 * - 1 different-split record: same dataset (CIFAR-10) but different split (5-fold CV)
 * - Additional records for richer demo
 */

import { SeekDB } from './seekdb.js';
import { fork } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const seeds = [
  // === Comparable group 1: CIFAR-10, train/test 80/20, accuracy ===
  {
    name: 'ResNet-56 基线',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'accuracy',
    value: 93.2,
    status: 'completed',
    config: 'ResNet-56, SGD lr=0.1, momentum=0.9, weight_decay=5e-4, epochs=200, batch_size=128',
    notes: '基线实验，使用默认超参数。数据增强: RandomCrop + HorizontalFlip。',
  },
  {
    name: 'ResNet-56 + Cutout',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'accuracy',
    value: 94.7,
    status: 'completed',
    config: 'ResNet-56, SGD lr=0.1, momentum=0.9, weight_decay=5e-4, epochs=200, batch_size=128, Cutout(16px)',
    notes: '在基线基础上增加 Cutout 数据增强，accuracy 提升 1.5%。',
  },
  // === Different split: CIFAR-10, 5-fold CV, accuracy ===
  {
    name: 'ResNet-56 五折交叉验证',
    dataset: 'CIFAR-10',
    split: '5-fold CV',
    metric: 'accuracy',
    value: 92.8,
    status: 'completed',
    config: 'ResNet-56, SGD lr=0.1, momentum=0.9, weight_decay=5e-4, epochs=200, batch_size=128, 5-fold cross validation',
    notes: '使用五折交叉验证，平均 accuracy 92.8%。因划分不同，与 train/test 80/20 不可直接比较。',
  },
  // === Comparable group 2: ImageNet, train/val, top-1 accuracy ===
  {
    name: 'EfficientNet-B0 基线',
    dataset: 'ImageNet',
    split: 'train/val',
    metric: 'top-1 accuracy',
    value: 77.1,
    status: 'completed',
    config: 'EfficientNet-B0, RMSprop lr=0.01, epochs=350, batch_size=2048, 4 TPU cores',
    notes: 'ImageNet 基线实验。',
  },
  {
    name: 'EfficientNet-B0 + AutoAugment',
    dataset: 'ImageNet',
    split: 'train/val',
    metric: 'top-1 accuracy',
    value: 78.9,
    status: 'completed',
    config: 'EfficientNet-B0, RMSprop lr=0.01, epochs=350, batch_size=2048, 4 TPU cores, AutoAugment',
    notes: '增加 AutoAugment 后 top-1 accuracy 提升 1.8%。',
  },
  // === Different metric on same dataset+split ===
  {
    name: 'ResNet-56 + Cutout (loss)',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'test loss',
    value: 0.185,
    status: 'completed',
    config: 'ResNet-56, SGD lr=0.1, momentum=0.9, weight_decay=5e-4, epochs=200, batch_size=128, Cutout(16px)',
    notes: '与 accuracy 实验同配置，但记录的是 test loss。指标不同，与 accuracy 不可比较。',
  },
  // === Incomplete experiment ===
  {
    name: 'WideResNet-28-10 训练中',
    dataset: 'CIFAR-10',
    split: 'train/test 80/20',
    metric: 'accuracy',
    value: 95.1,
    status: 'running',
    config: 'WideResNet-28-10, SGD lr=0.1, momentum=0.9, weight_decay=5e-4, epochs=200, batch_size=128',
    notes: '实验仍在进行中，当前 epoch 150/200 的中间结果。状态为 running，不可与已完成实验比较。',
  },
];

async function main() {
  const db = new SeekDB();
  await db.init();

  const version = await db.getVersion();
  console.log(`seekdb version: ${version}`);

  if (await db.count()) {
    console.log('Existing records kept. Run this command in a fresh directory to load the demonstration.');
    return;
  }
  console.log('Seeding database with demo data (fictional)...');

  for (const s of seeds) {
    const r = await db.insert(s);
    console.log(`  ✓ ${r.name} (${r.dataset}, ${r.split}, ${r.metric}=${r.value})`);
  }

  const count = await db.count();
  console.log(`\nSeeded ${count} records.`);
}

if (process.send) {
  main().then(() => process.send(true), (err) => {
    console.error('Seed failed:', err);
    process.send(false);
  });
} else {
  // The native binding can keep its worker threads alive after the seed writes finish.
  const child = fork(fileURLToPath(import.meta.url));
  let succeeded = false;
  child.once('message', (ok) => {
    succeeded = ok === true;
    child.kill();
  });
  child.once('exit', () => {
    process.exitCode = succeeded ? 0 : 1;
  });
}
