# 实验对照账本

面向科研组会的实验记录工具。把实验指标、数据集划分和配置放在同一张账本里，便于回查结果，避免把不同口径的数值直接比较。

## 本地运行

使用 Node.js 22 或更新版本。已验证 Apple Silicon macOS 与 Node.js 24。首次安装或运行需要访问 npm 和 seekdb 的原生组件下载站点。

```bash
git clone --branch competition/qoder-experiment-ledger-20261107 https://github.com/SimonAN01/oceanbase-2026-powercontext.git
cd oceanbase-2026-powercontext/qoder
npm ci
npm run seed
npm start
```

访问 `http://127.0.0.1:3000`。`npm run seed` 在空账本载入 7 条虚构记录，已有记录会保留。应用默认只监听本机，数据库位于当前目录的 `.seekdb/`。Qoder Sites 展示项目、截图及复现步骤，完整账本在本机运行。

## 验证样例

1. 搜索 `Cutout`，再选择 `CIFAR-10`、`train/test 80/20` 和 `accuracy`，应找到 `ResNet-56 + Cutout`。
2. 清空关键词，在同一组中比较 `ResNet-56 基线`（93.2）和 `ResNet-56 + Cutout`（94.7），差值为 1.5 个百分点。加入五折交叉验证记录后，账本标记划分不同，并省略该记录与前两条的差值。
3. 在「新增实验」录入一条记录并保存。关闭服务、从同一目录重新执行 `npm start`，原记录仍可查询。
4. 运行 `npm test` 验证原生数据库版本、插入、英中文查询、组合筛选和可比性判断。`curl http://127.0.0.1:3000/api/version` 返回 `5.7.25-OceanBase seekdb-v1.4.0.0`。

## seekdb 的作用

`src/seekdb.js` 使用官方 `seekdb@1.4.0` 的 `SeekdbClient` 和内嵌 `test` 数据库。`experiments` 表保存实验、配置和备注，接口的筛选条件使用参数化 SQL；英文关键词通过 `MATCH … AGAINST` 查询全文索引，中文关键词通过 `INSTR` 查询子串。每次保存和回查都直接使用 seekdb。只有数据集、划分和指标相同，且两条记录都已完成时，应用才计算差值。

## Qoder 开发记录

Qoder CN CLI 在同一会话中完成对照规则、HTTP 服务、前端、样例数据，以及从初版存储到原生 seekdb 的改造。集成复核修正了数据库初始化、中文匹配和测试退出。示例实验值均为虚构。

[Qoder 会话摘录截图](evidence/qoder-development.png) · [真实运行截图](site/app-demo.png) · [seekdb-js](https://github.com/oceanbase/seekdb-js)
