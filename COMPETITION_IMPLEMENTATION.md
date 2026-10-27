# PowerContext 2026 参赛实现与验证

日期：2026-10-27。实现基于冻结版本 `9db1753434162c0aa7c9f1189a1722e617880a52`，保留原始提交历史。CSV 基线位于 `competition/csv-evidence`；集成实现及 PDF 检索改进位于 `competition/pdf-financial-recall-20261027`。

## 功能

| 赛题 | 实现 | 已执行的公开自测 |
| --- | --- | --- |
| 仓库准备，10 分 | 保留冻结基线历史，仓库可匿名读取 | `check-baseline`；匿名 GitHub 仓库及指定 commit 查询 |
| CSV 行证据，20 分 | `sources/csv_rows.py`：完整校验、稳定行身份、原始字段及文本投影 | 17 项通过 |
| Pi 会话，20 分 | `sources/pi_session.py`：完整事件树校验、可见内容、分支切换、持久化 checkpoint 及历史改写检测 | 15 项通过 |
| PDF 页检索，50 分 | `sources/pdf_pages.py`：全部物理页、空白页、空密码权限、全文摘要、页级 Source；公开 Memory 搜索、数值问题的报表候选补全、公式词项扩展、跨行标题、离线重排及按实际 Source 去重 | 6 项通过；完整解析并持久化 503 页公开财报 |
| HTTP 重排，20 分 | `builtin/artifacts/memory/http_reranker.py`：原始全局序号、有限批次、响应严格校验、稳定排序和真实 usage | 10 项通过 |
| 增量上下文，20 分 | `builtin/runtime/context_pack.py` 及 `prepare_incremental`：UTF-8 分块、规范 JSON/Base64、摘要、协议字节数和独立重建 | 14 项通过 |
| 计数触发，20 分 | Runtime 配置及原生 supervisor：按 scope/family 成功 cursor 后的 journal 身份计数，与时间条件取 OR；保留原有窗口、wave 和重试 | 17 项通过 |
| Embedding 合并，20 分 | `builtin/inference/coalescing.py`：跨调用去重、执行中共享、LRU、失败隔离、取消隔离、逐批 usage | 8 项通过；6 个 scope 的 24 条共享文本实际只调用底层 4 次 |
| 来源生命周期，20 分 | `builtin/runtime/source_lifecycle.py`：持久化 DAG、all/any、版本与事件序号、原生 Memory 停用/恢复、Topic 搜索资格、历史审计及失败重放 | 4 项通过 |
| 证据选择，20 分 | `builtin/runtime/evidence_selection.py`：查询驱动的原句选择、相邻句合并、原字符区间、跨 scope 身份及完整 UTF-8 envelope 预算 | 3 项通过 |
| 单 SQL 混合检索，20 分 | `OceanBaseMemoryHybridIndex`：同一 SQL 取 FTS 与向量候选，保持原有融合、当前 head 校验和标签过滤 | 1 项通过；真实 seekdb `EXPLAIN` 同时含全文与向量索引 |

上述 10 个代码题自测共 **95 项通过**，使用 `competition/requirements.txt` 的固定 Linux x86_64 / Python 3.12 环境执行。公开自测文件和输入保持原样。

另增加了项目回归，检查生命周期传播中途发生真实 Memory 写入异常后的重启恢复、完成事件重放不产生新 Memory revision、Unicode/JSON 引用预算、不同 scope 的相同身份，以及长回答格式指令不遮蔽财务报表检索。PDF 的 9 项回归通过真实 Source observation、入库和 Memory 回查，覆盖同页多个条目、非 PDF 条目排除、显式报表优先、跨行标题、P&L / statements of operations 等报表别名、ROA/ROE 公式输入及未指明报表名称的 FCF 问题。HTTP 的 `evidence_selection` 字段从 OpenAPI 生成，默认关闭。

## 40 轮传输成本测量

这是一条本地验证序列，独立于平台隐藏测量。输入只取 [固定 QMSum revision 的 ES2004a 会话原文](https://github.com/Yale-LILY/QMSum/blob/83d7768c1f2b4dfeb091385d3dc7e239b8e5bb7e/data/ALL/test/ES2004a.json) 中包含 remote 或 design 的可见发言；不使用摘要、答案或证据标注。开始时入库 8 条真实发言，在第 10、20、30 轮各追加一条，第 20 轮把查询从 remote 改为 design，预算为 8,000 bytes。

40 轮均非空，共产生 4 种不同 Context。接收端逐轮仅用序列化 Pack/Delta 重建，所有正文与普通 prepare 逐字节一致。

| 计量 | 累计 bytes |
| --- | ---: |
| 普通完整正文 | 151,870 |
| 完整 v1 Pack | 209,890 |
| 每轮独立 gzip，level=9、mtime=0 | 66,740 |
| 首次 Pack 加后续全部 Delta | 30,514 |

增量传输相对原文节省 **79.91%**，相对完整 v1 Pack 节省 **85.46%**，相对本序列的逐轮 gzip 节省 **54.28%**。计量包含首次完整包、JSON、Base64、摘要和全部协议字段。这些数值只适用于上述输入和条件。

## 运行检查

```bash
# 官方固定依赖；需要受支持的 Linux x86_64 / Python 3.12 环境。
make -C competition install
make -C competition check-baseline
make -C competition test

# 项目开发环境和质量检查；与上游 CI 使用相同 uv 版本。
uv sync --locked --group dev --extra pdf
uv run prek run -a
uv run ty check src tests
uv run python scripts/generate_api.py --check
uv run python scripts/generate_js_operations.py --check
```

`competition/` 是随题发布的外部判题材料，项目格式化和类型检查不改写这些文件；它们通过各自的固定依赖自测入口执行。产品源码、项目自有测试和生成的 API 仍参加项目检查。新增生命周期持久化表已纳入中英文恢复文档的外键依赖顺序。

## 正式评分边界

总分上限为 240。[提交 `83b3079`](https://github.com/SimonAN01/oceanbase-2026-powercontext/commit/83b307987fd7aa8a0eeb49041f0616bf928fafd2) 的正式成功评测已确认 **190 / 240**：除 PDF 外的十题均获满分，PDF 为 0 / 50。PDF 检索改进的完整文档质量和各组 evidence-page Recall@20 仍需平台验收；公开测试通过不等于正式得分。任何排队或失败任务均没有可引用的成功分数。

Cookie、个人回执、真实任务编号和完整榜单仅保存在使用者本地状态目录，不属于本仓库。
