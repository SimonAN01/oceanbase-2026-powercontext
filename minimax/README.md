# 复习队列 SDD 助手

用规格驱动的小任务验证三项技术的分工。seekdb 的全文索引检索适用规格、实现和测试。PowerContext 保存阶段、版本摘要和后续动作。MiniMax 根据实际检索结果为目标文件生成候选代码。功能目标是为复习队列增加截止日过滤，同时保持原有调用行为。

## 环境

- Node.js 22.12+，seekdb-js 1.4.0。执行 `npm ci`。首次使用嵌入式 seekdb 会下载原生库。
- PowerContext 官方服务。项目使用默认 SQLite，相关操作为创建 scope、记忆写入和记忆列表。可按 [HTTP 快速开始](https://powercontext.oceanbase.io/zh/docs/develop/api-quickstart/) 安装，并为本例启动独立实例。
- 配置已授权的 MiniMax 竞赛凭证到 `MINIMAX_API_KEY` 环境变量。默认使用中国站 `MiniMax-M3`。国际站设置 `MINIMAX_REGION=international`，可用模型由 `MINIMAX_MODEL` 指定。
- 也可以通过魔搭 API-Inference 调用 MiniMax。设置 `SDD_MODEL_PROVIDER=modelscope` 和 `MODELSCOPE_API_KEY`，默认模型为 `MiniMax/MiniMax-M1-80k`。访问令牌可在 [魔搭账户设置](https://modelscope.cn/my/myaccesstoken) 获取。若推理接口提示邮箱验证，可在 [账号设置](https://modelscope.cn/my/settings/account) 完成。

在本目录启动 PowerContext：

```sh
POWERCONTEXT_HOME="$PWD/.sdd/powercontext" powercontext server run --host 127.0.0.1 --port 4327 --no-env-file
```

`POWERCONTEXT_URL` 默认是 `http://127.0.0.1:4327`。启用服务鉴权时通过 `POWERCONTEXT_TOKEN` 提供凭证。凭证不进入上下文包或运行记录。

## 验证任务

另开终端，在本目录执行：

```sh
node sdd.mjs index
node sdd.mjs baseline
node sdd.mjs context
node sdd.mjs generate
```

`index` 把五个有路径的工程资料写入 seekdb。检索应返回 `spec/review-queue.md`、`src/queue.mjs`、`test/baseline.test.mjs` 和 `test/due.test.mjs`。无关的 `spec/import-format.md` 不应入选。修改规格文字、再次索引和查询，可以核对检索使用了新文本。初始代码通过回归测试，新增日期过滤测试失败。

`generate` 会将样例工程中选出的上下文发送给 MiniMax。候选输出保存在 `.sdd/proposed-queue.mjs`，推理入口、响应中的模型 ID、请求 ID、用量、提示和候选摘要保存在 `.sdd/minimax-generation.json`。查看候选源文件后再执行以下命令，测试会在隔离目录运行：

```sh
node sdd.mjs verify
node sdd.mjs status
```

`verify` 使用原回归测试和新增功能测试，并保存命令、退出码和测试输出。两个测试都通过时，PowerContext 记录 `verified`。重启 PowerContext 或在新进程运行 `status`，都可以重新读取阶段。当前规格、代码与测试的内容摘要匹配时，`generate` 直接恢复已验证结果。重新索引后，变更的资料需要新的生成与验证。另设 `SDD_SCOPE_KEY` 和 `SDD_RUN_DIR` 可用新任务实例重跑。

## 当前验证进度

seekdb 索引、分类型全文检索、原测试基线、新功能红测和 PowerContext 状态读写已在本机完成。真实 MiniMax 代码生成、候选代码验证和提交材料归档待竞赛模型访问就绪后执行。
