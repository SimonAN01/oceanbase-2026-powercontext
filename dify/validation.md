# 验证记录

2026-11-07，macOS ARM64、Node.js 24.21.0、seekdb-js 1.4.0。

`npm test` 通过。测试使用真实嵌入式 seekdb，验证开案前请求、线索不足时的指认、同档案并行写入、误判计数、进程重启恢复、结案和档案隔离。

用 Dify 1.19.0 锁定的工作流引擎 Graphon `4fa22141774dff80e90e4dfa98ea3e6fbf0421bb` 加载 `lunar-case.yml`。仅将 `GAME_URL` 换成本机回合服务地址，实际执行 DSL 的表单输入、Jinja 模板、HTTP 请求和输出节点。同一全新档案依次运行：

| 输入行动 | 工作流结果 | 实际输出中核对的内容 |
| --- | --- | --- |
| 开始或继续 | GraphRunSucceededEvent | 线索 0/3 |
| 调查：温室 | GraphRunSucceededEvent | 三角形磁封 |
| 调查：维修间 | GraphRunSucceededEvent | 轮子拆下清洁 |
| 指认：运货机 | GraphRunSucceededEvent | 误判 1 |
| 指认：冰柜机 | GraphRunSucceededEvent | 已结案 |

复现上述工作流检查：先运行 `npm start`，另开终端，进入此目录运行下列命令。Python 使用 3.12 或更高版本。

```sh
uv run --python 3.12 --with 'graphon @ git+https://github.com/langgenius/graphon@4fa22141774dff80e90e4dfa98ea3e6fbf0421bb' --with jinja2 python - <<'PY'
from pathlib import Path
from uuid import uuid4

from graphon.dsl import loads
from graphon.engine_events.graph import GraphRunSucceededEvent

dsl = Path("lunar-case.yml").read_text().replace(
    "http://lunar-case-game:4317", "http://127.0.0.1:4317"
)
ticket = f"check-{uuid4().hex}"
for action, expected in [
    ("开始或继续", "线索 0/3"),
    ("调查：温室", "三角形磁封"),
    ("调查：维修间", "轮子拆下清洁"),
    ("指认：运货机", "误判 1"),
    ("指认：冰柜机", "已结案"),
]:
    engine = loads(dsl, start_inputs={"ticket": ticket, "action": action})
    event = list(engine.run())[-1]
    assert isinstance(event, GraphRunSucceededEvent), event
    assert expected in event.outputs["journal"], event.outputs
    print(action, type(event).__name__, expected)
PY
```

## Dify 1.19.0 自托管复验

在 Linux ARM64 的完整 Dify 1.19.0 Compose 环境中，通过工作室的导入接口载入原始 `lunar-case.yml`，再调用工作室草稿运行接口。保留 DSL 默认的内部 `GAME_URL`，由 Dify HTTP 节点和 SSRF 代理访问游戏容器。同一新档案的五次运行均以 `succeeded` 完成，分别核对了上表中的五段手记，包括误判计数和结案。

修订后的游戏镜像以全新容器和数据卷在独立内部网络启动，`GET /health` 返回成功。镜像在构建阶段安装 `libaio1` 并下载 seekdb 原生库，启动时预载原生库，容器可在没有外部网络的情况下使用本地数据库。
