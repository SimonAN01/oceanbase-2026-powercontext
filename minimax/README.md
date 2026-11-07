# 复习队列 SDD 助手

用规格驱动的小任务验证三项技术的分工。seekdb 的全文索引检索适用规格、实现和测试。PowerContext 保存阶段、版本摘要和后续动作。MiniMax 根据实际检索结果为目标文件生成候选代码。功能目标是为复习队列增加截止日过滤，同时保持原有调用行为。

## 环境

- Node.js 22.12+，seekdb-js 1.4.0。执行 `npm ci`。首次使用嵌入式 seekdb 会下载原生库。
- PowerContext 官方服务，实测版本 `0.1.dev178+g0e0016a06.d20261028`，使用默认 SQLite，通过 HTTP 创建 scope、写入记忆和读取阶段。可按 [HTTP 快速开始](https://powercontext.oceanbase.io/zh/docs/develop/api-quickstart/) 安装。
- 配置已授权的 MiniMax 竞赛凭证到 `MINIMAX_API_KEY` 环境变量。默认使用中国站 `MiniMax-M3`。国际站设置 `MINIMAX_REGION=international`，可用模型由 `MINIMAX_MODEL` 指定。
- 也可以通过魔搭 API-Inference 调用 MiniMax。设置 `SDD_MODEL_PROVIDER=modelscope` 和 `MODELSCOPE_API_KEY`，默认模型为 `MiniMax/MiniMax-M1-80k`。访问令牌可在 [魔搭账户设置](https://modelscope.cn/my/myaccesstoken) 获取。若推理接口提示邮箱验证，可在 [账号设置](https://modelscope.cn/my/settings/account) 完成。

在本目录启动 PowerContext：

```sh
POWERCONTEXT_HOME="$PWD/.sdd/powercontext" powercontext server run --host 127.0.0.1 --port 4327 --no-env-file
```

`POWERCONTEXT_URL` 默认是 `http://127.0.0.1:4327`。启用服务鉴权时通过 `POWERCONTEXT_TOKEN` 提供凭证。凭证不进入上下文包或运行记录。

## 验证样例

任务 `REVIEW-17` 为复习队列新增可选截止日。截止当天计入队列，未传截止日时保留原来的活跃卡片筛选和输入顺序。初始代码、规格和固定测试位于 `sample/`。在 `minimax/` 目录另开终端运行：

```sh
node sdd.mjs index
node sdd.mjs baseline
node sdd.mjs context
```

`index` 把五份工程资料写入 seekdb。`context` 按规格、代码和测试三类分别执行全文检索，输出来源路径、原文、匹配分、待办和已有验证结果。应返回 `spec/review-queue.md`、`src/queue.mjs`、`test/baseline.test.mjs` 和 `test/due.test.mjs`。`baseline` 记录原回归退出码 `0`，以及截止日功能断言失败的退出码 `1`。

配置模型访问凭证后，运行 `node sdd.mjs generate`，将检索结果与 PowerContext 进展发给 MiniMax。若要直接验证已归档的真实生成结果，可跳过调用模型，执行 `cp evidence/proposed-queue.mjs evidence/minimax-generation.json .sdd/`。两种方式都用以下命令验证：

```sh
node sdd.mjs verify
node sdd.mjs status
env -u MINIMAX_API_KEY -u MODELSCOPE_API_KEY node sdd.mjs generate
```

`verify` 将候选代码与原测试复制到隔离目录。两项测试通过后，PowerContext 保存 `verified`。新进程执行 `status` 会从服务读回阶段，随后 `generate` 在资料摘要匹配时返回 `resumed: true`。

要验证规格更新，可在样例规格里追加 `The review queue keeps encounter order under the due cutoff.`，再次执行 `index` 和 `context`，核对规格正文和 `contextVersion` 均发生变化。验证后恢复规格并重新索引。要从空状态重跑，在所有 CLI 调用前将 `SDD_RUN_DIR` 设为新目录，并将 `SDD_SCOPE_KEY` 设为新值。使用自定义运行目录时，将上述复制命令的目标改为该目录。

## 技术说明与验证记录

CLI 先通过 seekdb 1.4.0 的 `MATCH … AGAINST` 取得工程原文和路径，再把它们与 PowerContext 中的任务进展组合成上下文。MiniMax 根据该上下文生成目标文件。测试命令校验生成文件摘要、执行固定用例，并将验证结果写回 PowerContext。任务恢复比较当前四项资料的摘要，内容变化时重新走生成流程。

- 修改前代码见 [样例原文件](sample/src/queue.mjs)，修改后见 [MiniMax 生成文件](evidence/proposed-queue.mjs) 和 [代码差异](evidence/review-17.patch)。
- [MiniMax 调用记录](evidence/minimax-generation.json) 包含模型响应 ID、实际提示、seekdb 检索结果和代码摘要。归档时提取代码正文，保留模型返回的 JavaScript。
- [验证记录](evidence/validation.json) 包含同一组测试修改前后的命令、退出码和代码摘要，以及规格重建索引、PowerContext 写入和新进程复用的记录。
