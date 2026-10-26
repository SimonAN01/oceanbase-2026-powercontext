# CSV 行级采集与原始记录回查

本实现对应 PowerContext 2026 公开训练营的 `source-01-table-evidence` 题，基于官方 `2026-competition` 分支和冻结基线 `9db1753434162c0aa7c9f1189a1722e617880a52`。团队仓库原有说明保留在 `TEAM_README.md`，原有提交也保留在 Git 历史中。

## 实现

`src/powercontext/sources/csv_rows.py` 提供题目要求的输入模型、CSV 解析函数、行级 Source Definition 和标准 TextEvidence 投影：

- 严格 UTF-8 解码，接受开头的 BOM；保留引号内的换行、字段空格、列顺序和所有字符串值。
- 整个文档验证成功后才返回 tuple；拒绝错误表头、重复或空白主键、不匹配的字段数和格式错误。
- 文档 ID 和主键值分别百分号编码，形成稳定且不超过 256 字符的行 identity。
- 每行均为 CAPTURED Source，经现有 manifest/observation 与 Runtime ingestion 持久化。
- 标准投影支持原有 Memory 处理与精确 MemoryCitation 回查，无需模型、网络服务或新增依赖。

## 验证

官方验收使用独立的固定依赖环境：

```bash
make -C competition install
make -C competition check-baseline
make -C competition test-csv
```

首次安装后，重复同步固定依赖可以直接执行：

```bash
uv pip sync --python competition/.venv/bin/python competition/requirements.txt
```

补充测试包含 Unicode、编码后长度边界、严格错误输入、原值序列化，以及两个独立 Python 进程之间的持久化与精确引用回查。使用与官方验收相同的依赖运行：

```bash
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
  competition/.venv/bin/python -m pytest -q \
  tests/test_csv_rows.py tests/e2e/test_csv_rows_process_restart.py
```

产品开发环境与已有 Source 回归测试：

```bash
uv sync --frozen
uv run --frozen python -m pytest -q \
  tests/test_csv_rows.py tests/e2e/test_csv_rows_process_restart.py \
  tests/test_sources.py tests/builtin/persistence/test_sources.py \
  tests/builtin/persistence/test_generation_sources.py
uv run --frozen ty check src tests
```

官方 `competition/` 的题面、测试、样例与依赖保持原样。全仓库 `prek run -a` 会同时检查尚未实现的其他赛题接口和官方测试夹具，因此不能把它的整体结果作为本题的功能验收；提交时仍应如实报告其结果。

## 提测信息

代码题要求提交团队产品仓库 HTTPS URL 和包含实现的完整 40 位 commit SHA。本题分支为 `competition/csv-evidence`；提测时使用该分支的实际 commit，不能依赖尚未更新的默认分支。私有仓库还须按官方比赛平台的方式授予读取权限。

本地通过不等于正式评测通过，最终成绩以平台验收结果为准。

官方说明：[公开自测](competition/README.md)、[CSV 题面](competition/challenges/source-01-table-evidence/instruction.md)、[仓库来源要求](competition/challenges/setup-00-repository-fork/instruction.md)。
