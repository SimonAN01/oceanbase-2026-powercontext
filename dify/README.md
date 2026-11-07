# 月球失物局

月面温室种出的第一棵生菜在庆典前消失了。用一张档案号调查温室、维修间和广播室，把监控、维修记录与动机连起来，再指认嫌疑机器人。调查手记跟着档案号走，关掉页面后仍可继续。猜错会留下误判记录。

Dify 工作流收集档案号和行动，用模板节点生成 JSON，通过 HTTP 节点执行当前回合，再把返回的调查手记展示给玩家。seekdb 保存案件、线索和各档案的调查进度。回合服务用参数化 SQL 读取线索并写入进度，每轮展示的手记从 seekdb 回查。玩法使用可复查的固定规则，无需模型账号、插件、知识库或 API Key。

## 导入 Dify

使用 [Dify 1.19.0](https://github.com/langgenius/dify/tree/1.19.0) 的自托管 Docker Compose 部署和 Docker Compose v2.35 或更新版。后端镜像使用 Node.js 24 和 seekdb-js 1.4.0。构建时需要访问 Docker Hub、Debian、npm 和 seekdb 原生组件下载站点。镜像包含 `libaio1` 和原生组件，启动时直接加载。

1. 按 Dify 的 Docker README 准备 `docker/.env`，保留默认 Compose profiles 并生成部署要求的 sandbox token。新装的 Dify 在浏览器打开 `/install` 完成初始化。
2. 在 Dify 的 `docker/` 目录运行以下命令。把环境变量值替换为本作品 `dify/` 目录的绝对路径。

   ```sh
   export LUNAR_CASE_DIR=/absolute/path/to/competition-repo/dify
   docker compose -f docker-compose.yaml -f "$LUNAR_CASE_DIR/compose.game.yaml" up -d --build
   docker compose -f docker-compose.yaml -f "$LUNAR_CASE_DIR/compose.game.yaml" logs lunar-case-game
   ```

   如果构建机通过代理访问下载站，在同一组 Compose 文件上先执行 `build --build-arg HTTPS_PROXY lunar-case-game`，再执行 `up -d`。

   游戏就绪时日志显示 `Lunar Lost & Found ready on port 4317`。Compose 只在内部网络提供游戏服务，Dify 的 SSRF 代理通过 `SSRF_PROXY_ALLOW_PRIVATE_DOMAINS` 允许访问 `lunar-case-game`。运行数据保存在 `lunar-case-data` 卷。默认服务地址是 `http://lunar-case-game:4317`。
3. 在 Dify「工作室 → 导入 DSL 文件」选择 [lunar-case.yml](lunar-case.yml)。若使用单独部署的回合服务，在工作流变量中设置 `GAME_URL` 为 Dify 服务和其 SSRF 代理均可访问的 HTTP 地址。地址末尾省略斜杠。
4. 在工作室点击「运行」，输入档案号并选一个行动。工作流发布后也可以从 Dify Web App 入口游玩。

## 一局验证样例

每个档案号有自己的进度，第一次体验可选 `moon-demo-001`。再次完整验证时用一个新的档案号。应用为单实例部署，档案号是用于继续游戏的短码，请用随机短码并避免写入个人信息。

| 次序 | 行动 | 预期输出 |
| --- | --- | --- |
| 1 | 开始或继续 | 显示失踪事件，线索 0/3 |
| 2 | 调查：温室 | 监控发现三角形磁封 |
| 3 | 调查：维修间 | 看到磁封对照和机器人维修记录，线索 2/3 |
| 4 | 指认：运货机 | 指认错误，线索仍为 2/3，误判变为 1 |
| 5 | 重新打开页面，沿用档案号，选择「开始或继续」 | 继续显示两条已取得的线索和一次误判 |
| 6 | 指认：冰柜机 | 已结案，交代生菜去向 |
| 7 | 使用新档案号开始 | 新档案显示线索 0/3、误判 0 |

`npm ci && npm test` 可单独验证回合服务。测试启动真实 seekdb，将两个调查请求并发提交，重启进程，继续使用同一档案号结案并检查新档案。测试数据放在临时目录。也可执行 `npm start` 在 `127.0.0.1:4317` 单独运行后端。

[验证记录](validation.md)包含本机实测及工作流执行结果。[赛题说明](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/lhfslihtnnb8yqh6) · [Dify HTTP 节点](https://docs.dify.ai/en/cloud/use-dify/nodes/http-request) · [seekdb-js](https://github.com/oceanbase/seekdb-js)
