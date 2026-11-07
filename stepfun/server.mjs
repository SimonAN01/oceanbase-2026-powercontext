import { createHash } from "node:crypto";
import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, resolve, sep } from "node:path";
import { FulltextIndexConfig, Schema, SeekdbClient, VectorIndexConfig } from "seekdb";

const client = new SeekdbClient({ path: process.env.INDEX_DB || "./.seekdb", database: "test" });
const dimension = 384;
const schema = new Schema({
  fulltextIndex: new FulltextIndexConfig("ik", { ik_mode: "smart" }),
  vectorIndex: new VectorIndexConfig({
    hnsw: { dimension, distance: "cosine" },
    embeddingFunction: null,
  }),
});
const staticRoot = resolve("dist");
const contentTypes = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".wasm": "application/wasm",
};
// The SDK uses a session variable across the SQL statements of hybridSearch.
let databaseQueue = Promise.resolve();

function reply(response, status, value) {
  response.writeHead(status, { "Content-Type": "application/json; charset=utf-8" });
  response.end(JSON.stringify(value));
}

async function readJson(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 8 * 1024 * 1024) throw new RangeError("单次导入最多 8 MiB。");
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks));
}

function validVector(vector) {
  return Array.isArray(vector) && vector.length === dimension &&
    vector.every((value) => typeof value === "number" && Number.isFinite(value)) &&
    vector.some((value) => value !== 0);
}

async function handleApi(path, request) {
  if (!request.headers["content-type"]?.startsWith("application/json")) {
    throw new TypeError("需要 JSON 请求。");
  }
  if (request.headers.origin && request.headers.origin !== `http://${request.headers.host}`) {
    throw new TypeError("仅接受本地页面的请求。");
  }
  const body = await readJson(request);
  if (!body || typeof body.library !== "string" || !/^[a-z][a-z0-9_]{1,31}$/.test(body.library)) {
    throw new TypeError("知识库名称须以小写字母开头，长度为 2 至 32 字符。");
  }
  const name = `page_${body.library}`;
  if (path === "/api/import") {
    const rows = body.rows;
    if (!Array.isArray(rows) || rows.length === 0 || rows.length > 500 ||
        rows.some((row) => !row || typeof row.title !== "string" || !row.title.trim() ||
          row.title.length > 255 || typeof row.text !== "string" || !row.text.trim() ||
          row.text.length > 2400 || !Number.isSafeInteger(row.part) || row.part < 1 ||
          !validVector(row.vector))) {
      throw new TypeError("导入需要 1 至 500 个有效文本片段及 384 维向量。");
    }
    const library = await client.getOrCreateCollection({ name, schema, embeddingFunction: null });
    const unique = new Map();
    for (const row of rows) {
      const id = createHash("sha256").update(JSON.stringify([row.title, row.part, row.text])).digest("hex");
      unique.set(id, row);
    }
    const documents = [...unique.values()];
    await library.upsert({
      ids: [...unique.keys()],
      documents: documents.map((row) => row.text),
      embeddings: documents.map((row) => row.vector),
      metadatas: documents.map((row) => ({ title: row.title, part: row.part })),
    });
    await library.refresh_index();
    return { indexed: documents.length, total: await library.count() };
  }
  if (typeof body.query !== "string" || !body.query.trim() || body.query.length > 300 ||
      !validVector(body.vector)) {
    throw new TypeError("请输入 1 至 300 字符的查询。");
  }
  const library = await client.getOrCreateCollection({ name, schema, embeddingFunction: null });
  const start = performance.now();
  const result = await library.hybridSearch({
    query: { whereDocument: { $contains: body.query } },
    knn: { queryEmbeddings: [body.vector] },
    rank: { rrf: {} },
    nResults: 5,
    include: ["documents", "metadatas"],
  });
  const hits = (result.ids?.[0] || []).map((id, index) => ({
    id,
    title: result.metadatas?.[0]?.[index]?.title,
    part: result.metadatas?.[0]?.[index]?.part,
    text: result.documents?.[0]?.[index],
  }));
  return { hits, searchMs: Math.round(performance.now() - start) };
}

const server = createServer({ requestTimeout: 15_000 }, async (request, response) => {
  try {
    const path = new URL(request.url, "http://localhost").pathname;
    if (request.method === "POST" && ["/api/import", "/api/search"].includes(path)) {
      const query = databaseQueue.then(() => handleApi(path, request));
      databaseQueue = query.catch(() => {});
      reply(response, 200, await query);
      return;
    }
    const target = resolve(staticRoot, path === "/" ? "index.html" : `.${decodeURIComponent(path)}`);
    if (request.method !== "GET" || !target.startsWith(staticRoot + sep)) {
      reply(response, 404, { error: "找不到资源。" });
      return;
    }
    const data = await readFile(target);
    response.writeHead(200, {
      "Content-Type": contentTypes[extname(target)] || "application/octet-stream",
    });
    response.end(data);
  } catch (error) {
    if (error.code === "ENOENT") reply(response, 404, { error: "找不到资源。请先运行 npm run build。" });
    else if (error instanceof TypeError || error instanceof RangeError || error instanceof SyntaxError) {
      reply(response, 400, { error: error.message });
    } else {
      console.error(error);
      reply(response, 500, { error: "seekdb 未完成请求。请检查服务终端。" });
    }
  }
});

const port = Number(process.env.PORT || 4173);
server.listen(port, "127.0.0.1", () => console.log(`页索引 http://127.0.0.1:${port}`));
