import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { SeekdbClient } from "seekdb";

const caseFile = JSON.parse(await readFile(new URL("./case.json", import.meta.url), "utf8"));
const client = new SeekdbClient({ path: process.env.GAME_DB || "./.seekdb", database: "test" });
await client.execute(`
  CREATE TABLE IF NOT EXISTS lunar_case_records (
    record_id VARCHAR(96) COLLATE utf8mb4_bin PRIMARY KEY,
    payload TEXT NOT NULL
  )
`);
const caseId = "case:lettuce";
const { clues, ...caseSummary } = caseFile;
const clueIds = Object.keys(clues).map((location) => `clue:${location}`);

async function putRecord(id, payload) {
  await client.execute(
    "INSERT INTO lunar_case_records (record_id, payload) VALUES (?, ?) ON DUPLICATE KEY UPDATE payload = VALUES(payload)",
    [id, payload],
  );
}

await putRecord(caseId, JSON.stringify(caseSummary));
for (const [location, clue] of Object.entries(clues)) await putRecord(`clue:${location}`, clue);

async function getRecord(id) {
  const rows = await client.execute("SELECT payload FROM lunar_case_records WHERE record_id = ?", [id]);
  return rows?.[0]?.payload;
}

async function turn({ ticket, action }) {
  if (typeof ticket !== "string" || !/^[A-Za-z0-9_-]{8,40}$/.test(ticket)) {
    return "档案号须为 8 至 40 个英文字母、数字、短横线或下划线。";
  }
  const id = `ticket:${ticket}`;
  const dossier = JSON.parse(await getRecord(caseId));
  const saved = await getRecord(id);
  if (!saved && action !== "开始或继续") return "请先用这个档案号选择「开始或继续」。";
  const state = saved ? JSON.parse(saved) : { inspected: [], misses: 0, solved: false };
  let event = state.solved ? dossier.solution : dossier.intro;

  if (!state.solved && typeof action === "string" && action.startsWith("调查：")) {
    const location = action.slice(3);
    const clueId = `clue:${location}`;
    if (!clueIds.includes(clueId)) return "请从行动列表选择调查地点。";
    event = await getRecord(clueId);
    if (!state.inspected.includes(location)) state.inspected.push(location);
  } else if (!state.solved && typeof action === "string" && action.startsWith("指认：")) {
    const suspect = action.slice(3);
    if (!Object.hasOwn(dossier.suspects, suspect)) return "请从行动列表选择一台机器人。";
    if (state.inspected.length < 2) {
      event = "失物局只接受有依据的指认。请至少调查两处地点。";
    } else {
      const hint = dossier.suspects[suspect];
      state.solved = hint === "";
      if (!state.solved) state.misses += 1;
      event = state.solved ? dossier.solution : hint;
    }
  } else if (!state.solved && action !== "开始或继续") {
    return "请从行动列表选择下一步。";
  }

  await putRecord(id, JSON.stringify(state));
  const journal = [];
  for (const location of state.inspected) journal.push(await getRecord(`clue:${location}`));
  return [
    dossier.title,
    "",
    event,
    "",
    `状态：${state.solved ? "已结案" : "调查中"} | 线索 ${journal.length}/${clueIds.length} | 误判 ${state.misses}`,
    "",
    "调查手记",
    ...journal.map((clue) => `• ${clue}`),
    ...(journal.length ? [] : ["暂无线索。"]),
    "",
    state.solved ? "换一个新档案号可以重新开局。" : "沿用档案号，选择下一步行动。",
  ].join("\n");
}

async function input(request) {
  let size = 0;
  const chunks = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > 4096) throw new RangeError("请求超过 4 KiB。");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new TypeError("请输入 JSON 对象。");
  return value;
}

// Serialize state changes within the single process serving this embedded database.
let queue = Promise.resolve();
const server = createServer({ requestTimeout: 10_000 }, async (request, response) => {
  response.setHeader("Content-Type", "text/plain; charset=utf-8");
  if (request.method === "GET" && request.url === "/health") {
    response.end("ready");
    return;
  }
  if (request.method !== "POST" || request.url !== "/turn") {
    response.writeHead(404).end("请使用 POST /turn。");
    return;
  }
  try {
    const body = await input(request);
    const pending = queue.then(() => turn(body));
    queue = pending.catch(() => {});
    response.end(await pending);
  } catch (error) {
    if (error instanceof SyntaxError || error instanceof RangeError || error instanceof TypeError) {
      response.writeHead(400).end("请求格式无效。");
      return;
    }
    console.error(error);
    response.writeHead(500).end("失物局暂时无法读取档案。");
  }
});
server.listen(Number(process.env.PORT || 4317), process.env.HOST || "127.0.0.1", () => {
  console.log(`Lunar Lost & Found ready on port ${server.address().port}`);
});
