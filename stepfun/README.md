# 页索引

页索引在浏览器中把文本和查询编码为向量，再用本地 seekdb 做全文与语义混合检索。结果显示源文件名、片段序号和原文。

## 启动

使用 Node.js 22.12+ 和当前版 Chrome 或 Edge。seekdb-js 1.4.0 使用原生嵌入式组件，支持 macOS Apple Silicon、Linux x86_64 / ARM64 和 Windows x86_64。首次安装需下载依赖，首次打开知识库时浏览器需下载公开模型。

```sh
npm ci
npm run build
npm start
```

打开 `http://127.0.0.1:4173`。点击「导入英文示例」，检索 `How are committed changes recovered after a crash?`，检查结果是否包含 `write-ahead-log.md` 的原文。再导入自己的 TXT 或 Markdown，搜索新片段中的词语。将知识库名从 `notes` 换为 `other` 可验证数据隔离，切回 `notes` 可继续使用原资料。

服务仅监听本机。嵌入式数据库默认写入 `.seekdb/`，可用 `INDEX_DB` 指定其他目录，`PORT` 可指定端口。关闭和重启应用会保留索引。相同文件片段重导时按标题、片段序号和内容的 SHA-256 去重。

## 技术说明

- 模型为 Apache-2.0 的 Xenova/all-MiniLM-L6-v2，固定 revision `751bff37182d3f1213fa05d7196b954e230abad9`。Transformers.js 4.3.2 在浏览器生成 384 维归一化向量。公开模型文件只用于下载，文档和查询的推理都在页面内完成。模型主要面向英文。
- 优先加载 WebGPU FP16，初始化或预热失败时回到 WASM INT8。页面显示实际推理后端和精度。按同一 revision 的 ONNX 文件计算，FP32 为 90,387,606 字节，FP16 为 45,297,825 字节，INT8 为 22,972,370 字节，分别缩至约 1/2 和 1/3.93。
- 浏览器按最多 8 个片段组成一个推理批次，缓存已加载的模型。应用将正文、向量和元数据一次批量写入 seekdb-js 集合，并调用 `refresh_index()`。每个集合有 IK 全文索引和 HNSW 余弦索引。seekdb-js 的 `hybridSearch` 在数据库内执行两路检索与 RRF，页面按数据库返回顺序显示结果。
- 页面记录模型加载及预热、文档向量化、写入及索引更新、查询向量化、数据库检索和查询端到端耗时。追加 `?benchmark=1` 后点击「运行六问实测」，应用在独立集合导入六份示例并逐一检索，每问只有一个目标片段。追加 `&device=wasm&precision=fp32&batch=1` 可跑基线，追加 `&device=wasm` 可强制走 INT8，保留默认设置则尝试 WebGPU。页面附有每问原始计时。

### 六问实测

Apple M5、macOS 27.0.1，使用 Codex 内置浏览器和嵌入式 seekdb 1.4.0。各配置导入同一批 6 个片段。查询耗时为预热后 6 个不同问题的中位数，单位为毫秒。加载及预热包含各配置实际发生的模型下载，受缓存影响。

| 配置 | 批大小 | 加载及预热 | 文档向量化 | 写入及索引 | 查询向量 | seekdb 检索 | 端到端 | Recall@3 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| WASM FP32 | 1 | 48,583 | 117 | 555 | 7 | 1 | 12.5 | 6/6 |
| WASM INT8 | 8 | 5,904 | 88 | 454 | 8 | 3 | 15.5 | 6/6 |
| WebGPU FP16 | 8 | 23,645 | 96 | 505 | 22 | 1.5 | 37.5 | 6/6 |

这组小样本中，三种配置均命中全部目标片段，且每问目标的排名相同。INT8 批处理配置的文档向量化最快，FP32 基线的单问端到端中位数最短。浏览器默认路径实际启用了 WebGPU，禁用 WebGPU 后，WASM 路径仍能完成导入和检索。

参见模型和接口文档。[固定模型版本](https://huggingface.co/Xenova/all-MiniLM-L6-v2/tree/751bff37182d3f1213fa05d7196b954e230abad9) · [seekdb-js](https://github.com/oceanbase/seekdb-js/tree/v1.4.0)
