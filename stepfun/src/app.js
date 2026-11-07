import { env, pipeline } from "@huggingface/transformers";
import "./styles.css";

const model = "Xenova/all-MiniLM-L6-v2";
const revision = "751bff37182d3f1213fa05d7196b954e230abad9";
const params = new URLSearchParams(location.search);
const benchmarkBaseline = params.get("precision") === "fp32";
const batchSize = params.get("batch") === "1" ? 1 : 8;
const find = (id) => document.getElementById(id);
const libraryInput = find("library");
const libraryKey = "page-index-library";
const lastLibrary = localStorage.getItem(libraryKey);
if (lastLibrary && /^[a-z][a-z0-9_]{1,31}$/.test(lastLibrary)) libraryInput.value = lastLibrary;
libraryInput.addEventListener("change", () => {
  find("results").replaceChildren();
  find("search-status").textContent = `当前知识库：${libraryInput.value}`;
});
env.allowLocalModels = false;

let extractorPromise;
let busy = false;
const demo = [
  { title: "write-ahead-log.md", text: "A write-ahead log records changes before they reach data pages. After a crash the storage engine replays durable log records to restore committed changes." },
  { title: "vector-search.md", text: "Vector search retrieves documents by distance in an embedding space. A cosine metric compares the angle between dense vectors." },
  { title: "covering-index.md", text: "A covering index contains all columns needed by a query. The database can answer it directly from the index without reading table pages." },
  { title: "snapshot-isolation.md", text: "Snapshot isolation lets a transaction read a consistent snapshot. Concurrent writers can still cause write skew on different rows." },
  { title: "full-text-index.md", text: "A full-text index maps tokens to documents. The BM25 ranking formula considers document length and keyword frequency." },
  { title: "replication.md", text: "A database replica follows the primary by consuming the change stream. Asynchronous replicas may lag behind the latest commit." },
];
const demoQueries = [
  "How are committed changes recovered after a crash?",
  "How do you measure similarity between dense embeddings?",
  "When can a query avoid reading table pages?",
  "Why can concurrent transactions cause write skew?",
  "How does BM25 rank documents by their terms?",
  "Why can a read from a replica be stale?",
];

async function initialize() {
  const label = find("backend");
  const began = performance.now();
  let supportsGpu = false;
  if (params.get("device") !== "wasm") {
    try {
      supportsGpu = Boolean(await navigator.gpu?.requestAdapter());
    } catch {
      supportsGpu = false;
    }
  }
  const candidates = supportsGpu ? ["webgpu", "wasm"] : ["wasm"];
  for (const device of candidates) {
    const dtype = benchmarkBaseline ? "fp32" : device === "webgpu" ? "fp16" : "q8";
    label.textContent = `加载模型 · ${device.toUpperCase()} ${dtype.toUpperCase()}`;
    let extractor;
    try {
      extractor = await pipeline("feature-extraction", model, { revision, device, dtype });
      await extractor(["Warm-up"], { pooling: "mean", normalize: true });
      const load = Math.round(performance.now() - began);
      label.textContent = `${device.toUpperCase()} · ${dtype.toUpperCase()} · 加载及预热 ${load} ms`;
      return extractor;
    } catch (error) {
      await extractor?.dispose();
      if (device === "wasm") {
        label.textContent = "模型加载失败";
        throw error;
      }
    }
  }
}

async function getExtractor() {
  extractorPromise ??= initialize().catch((error) => {
    extractorPromise = undefined;
    throw error;
  });
  return extractorPromise;
}

async function encode(extractor, texts) {
  const result = await extractor(texts, { pooling: "mean", normalize: true });
  return result.tolist();
}

async function request(path, data) {
  const response = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(data),
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error);
  return body;
}

async function withUi(statusId, action) {
  if (busy || !libraryInput.reportValidity()) return;
  busy = true;
  const library = libraryInput.value;
  localStorage.setItem(libraryKey, library);
  const controls = [libraryInput, ...document.querySelectorAll("button")];
  controls.forEach((control) => { control.disabled = true; });
  const status = find(statusId);
  try {
    await action(library, status);
  } catch (error) {
    status.textContent = `操作失败：${error.message}`;
  } finally {
    controls.forEach((control) => { control.disabled = false; });
    busy = false;
  }
}

async function ingest(documents, library, status) {
  const rows = [];
  for (const { title, text } of documents) {
    const characters = Array.from(text.trim());
    for (let offset = 0, part = 1; offset < characters.length; offset += 720, part += 1) {
      const fragment = characters.slice(offset, offset + 720).join("").trim();
      if (fragment) rows.push({ title, text: fragment, part });
    }
  }
  if (!rows.length || rows.length > 500) throw new Error("请选择含文字的文件，每批最多 500 个片段。");
  const progress = find("progress");
  progress.max = rows.length;
  progress.value = 0;
  status.textContent = "正在准备本地模型…";
  const extractor = await getExtractor();
  const started = performance.now();
  for (let index = 0; index < rows.length; index += batchSize) {
    const batch = rows.slice(index, index + batchSize);
    const vectors = await encode(extractor, batch.map((row) => row.text));
    batch.forEach((row, offset) => { row.vector = vectors[offset]; });
    progress.value = index + batch.length;
    status.textContent = `向量化进度 ${progress.value}/${rows.length}`;
  }
  const embeddingMs = Math.round(performance.now() - started);
  const writeStarted = performance.now();
  status.textContent = "写入 seekdb 并更新索引…";
  const result = await request("/api/import", { library, rows });
  const indexMs = Math.round(performance.now() - writeStarted);
  status.textContent = `已索引 ${result.indexed} 个片段，库内共 ${result.total} 个。向量化 ${embeddingMs} ms，写入及索引更新 ${indexMs} ms。`;
  return { embeddingMs, indexMs };
}

async function search(library, query) {
  const start = performance.now();
  const extractor = await getExtractor();
  const encodingStarted = performance.now();
  const [vector] = await encode(extractor, [query]);
  const encodingMs = Math.round(performance.now() - encodingStarted);
  const { hits, searchMs } = await request("/api/search", { library, query, vector });
  return { hits, searchMs, encodingMs, totalMs: Math.round(performance.now() - start) };
}

find("demo").addEventListener("click", () => withUi("import-status", (library, status) => ingest(demo, library, status)));
find("import").addEventListener("submit", (event) => {
  event.preventDefault();
  withUi("import-status", async (library, status) => {
    const files = [...find("files").files];
    if (!files.length || files.reduce((bytes, file) => bytes + file.size, 0) > 500_000) {
      throw new Error("请选择文本文件，单批源文件合计最多 500 KB。");
    }
    const documents = await Promise.all(files.map(async (file) => ({ title: file.name, text: await file.text() })));
    await ingest(documents, library, status);
  });
});

find("search").addEventListener("submit", (event) => {
  event.preventDefault();
  withUi("search-status", async (library, status) => {
    const query = find("query").value.trim();
    if (!query) throw new Error("请输入问题或关键词。");
    const results = find("results");
    results.replaceChildren();
    status.textContent = "正在检索…";
    const { hits, searchMs, encodingMs, totalMs } = await search(library, query);
    for (const hit of hits) {
      const item = document.createElement("li");
      const title = document.createElement("h3");
      const text = document.createElement("p");
      title.textContent = `${hit.title} · 片段 ${hit.part}`;
      text.textContent = hit.text;
      item.append(title, text);
      results.append(item);
    }
    status.textContent = hits.length
      ? `${hits.length} 个片段 · 查询向量 ${encodingMs} ms · seekdb ${searchMs} ms · 总计 ${totalMs} ms`
      : "当前知识库没有匹配片段。可以导入资料或换一种问法。";
  });
});

if (params.has("benchmark")) {
  const control = document.createElement("button");
  control.className = "secondary";
  control.type = "button";
  control.textContent = "运行六问实测";
  find("import").append(control);
  control.addEventListener("click", () => withUi("search-status", async (library, status) => {
    const benchmarkLibrary = `bench_${crypto.randomUUID().slice(0, 8)}`;
    const load = await ingest(demo, benchmarkLibrary, find("import-status"));
    let correct = 0;
    const samples = [];
    for (const [index, query] of demoQueries.entries()) {
      status.textContent = `正在测试第 ${index + 1}/${demoQueries.length} 问…`;
      const { hits, ...timings } = await search(benchmarkLibrary, query);
      const rank = hits.findIndex((hit) => hit.title === demo[index].title) + 1;
      samples.push({ rank, ...timings });
      if (rank >= 1 && rank <= 3) correct += 1;
    }
    status.textContent = `实测完成 · Recall@3 ${correct}/${demoQueries.length}`;
    const result = document.createElement("pre");
    result.textContent = JSON.stringify({ backend: find("backend").textContent, batchSize, ...load, samples }, null, 2);
    find("results").replaceChildren();
    const row = document.createElement("li");
    row.append(result);
    find("results").append(row);
  }));
}
