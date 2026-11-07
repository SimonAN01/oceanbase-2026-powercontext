import { createHash } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import { resolve } from "node:path";
import { fork, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { SeekdbClient } from "seekdb";

const stagePrefix = "review-17 progress ";
const query = "review queue due cutoff";
const records = [
  ["spec", "spec/review-queue.md"], ["spec", "spec/import-format.md"],
  ["code", "src/queue.mjs"], ["test", "test/baseline.test.mjs"], ["test", "test/due.test.mjs"],
];
const root = new URL("./sample/", import.meta.url);
const runRoot = resolve(process.env.SDD_RUN_DIR || ".sdd");
const target = "src/queue.mjs";
const hash = (value) => createHash("sha256").update(value).digest("hex");
const powercontext = process.env.POWERCONTEXT_URL || "http://127.0.0.1:4327";

async function post(base, path, body, headers = {}) {
  const response = await fetch(new URL(path, base), {
    method: "POST",
    headers: { "Content-Type": "application/json", ...headers },
    body: JSON.stringify(body),
    redirect: "error",
    signal: AbortSignal.timeout(90_000),
  });
  if (!response.ok) throw new Error(`${new URL(base).hostname} returned HTTP ${response.status}`);
  return response.json();
}

function memory(path, body) {
  const token = process.env.POWERCONTEXT_TOKEN;
  return post(powercontext, path, body, token ? { Authorization: `Bearer ${token}` } : {});
}

async function scope() {
  const data = await memory("/v1/scopes", {
    title: "Review queue SDD",
    summary: "Code and verification progress for a synthetic flashcard business task",
    idempotency_key: process.env.SDD_SCOPE_KEY || "review-queue-sdd-review-17",
  });
  return data.scope_id;
}

async function status() {
  const scopeId = await scope();
  const result = await memory("/v1/memory/entries/list", { scope_id: scopeId });
  return result.entries.filter((entry) => entry.text.startsWith(stagePrefix))
    .map((entry) => ({ ...JSON.parse(entry.text.slice(stagePrefix.length)), citation: entry.citation }))
    .sort((a, b) => b.recordedAt.localeCompare(a.recordedAt));
}

async function remember(stage, detail) {
  const scopeId = await scope();
  const state = { stage, recordedAt: new Date().toISOString(), ...detail };
  const response = await memory("/v1/memory/remember", {
    scope_id: scopeId, kind: "decision", text: stagePrefix + JSON.stringify(state),
    reason: "Record the outcome of an explicit SDD workflow action",
  });
  return { ...state, powercontext: response };
}

async function withDatabase(action) {
  await mkdir(runRoot, { recursive: true });
  const db = new SeekdbClient({ path: resolve(runRoot, "seekdb"), database: "test" });
  try {
    await db.execute(`CREATE TABLE IF NOT EXISTS sdd_documents (
      path VARCHAR(128) PRIMARY KEY,
      category VARCHAR(12) NOT NULL,
      body TEXT NOT NULL,
      FULLTEXT KEY idx_body (body) WITH PARSER space
    )`);
    return await action(db);
  } finally {
    await db.close();
  }
}

function index() {
  return withDatabase(async (db) => {
    for (const [category, path] of records) {
      const body = await readFile(new URL(path, root), "utf8");
      await db.execute(
        "INSERT INTO sdd_documents (path, category, body) VALUES (?, ?, ?) ON DUPLICATE KEY UPDATE body = VALUES(body)",
        [path, category, body],
      );
    }
    return { indexed: records.length };
  });
}

async function context() {
  const latest = (await status())[0] || null;
  const sources = await withDatabase(async (db) => {
    const selected = [];
    for (const category of ["spec", "code", "test"]) {
      const hits = await db.execute(`SELECT path, body, MATCH(body) AGAINST (? IN NATURAL LANGUAGE MODE) AS score
        FROM sdd_documents WHERE category = ? AND MATCH(body) AGAINST (? IN NATURAL LANGUAGE MODE)
        ORDER BY score DESC, path LIMIT 3`, [query, category, query]);
      if (!hits?.length) throw new Error(`No ${category} matched. Run the index command first.`);
      for (const { path, body, score } of hits) selected.push({ category, path, body, score });
    }
    return selected;
  });
  const versions = sources.map(({ path, body }) => [path, hash(body)]).sort(([a], [b]) => a.localeCompare(b));
  return { task: "REVIEW-17", query, target, sources, contextVersion: hash(JSON.stringify(versions)), latest };
}

async function generate() {
  const pack = await context();
  const previous = pack.latest;
  if (previous?.stage === "verified" && previous.contextVersion === pack.contextVersion) {
    return { resumed: true, ...previous };
  }
  const provider = process.env.SDD_MODEL_PROVIDER || "minimax";
  if (provider !== "minimax" && provider !== "modelscope") throw new Error("Choose minimax or modelscope.");
  const useModelScope = provider === "modelscope";
  const keyName = useModelScope ? "MODELSCOPE_API_KEY" : "MINIMAX_API_KEY";
  const key = process.env[keyName];
  if (!key) throw new Error(`${keyName} is required for a new MiniMax generation.`);
  const model = process.env.MINIMAX_MODEL || (useModelScope ? "MiniMax/MiniMax-M3" : "MiniMax-M3");
  const host = useModelScope ? "https://api-inference.modelscope.cn"
    : process.env.MINIMAX_REGION === "international" ? "https://api.minimax.io" : "https://api.minimax.cn";
  const endpoint = useModelScope ? "/v1/chat/completions" : "/anthropic/v1/messages";
  const headers = useModelScope ? { Authorization: `Bearer ${key}` }
    : { "x-api-key": key, "anthropic-version": "2023-06-01" };
  const { citation: _citation, ...progress } = previous || {};
  const prompt = "Implement REVIEW-17 using this context. Only rewrite src/queue.mjs. Return its full JavaScript source, with no explanatory prose. " +
    "Use only standard JavaScript. Existing test and spec documents describe the requirements. Historical progress is context only.\n\n" +
    JSON.stringify({ ...pack, latest: previous ? progress : null });
  const response = await post(host, endpoint, {
    model, max_tokens: useModelScope ? 8192 : 4096, messages: [{ role: "user", content: prompt }],
  }, headers);
  const choice = response.choices?.[0];
  const code = useModelScope ? choice?.message?.content
    : response.content?.filter((block) => block.type === "text").map((block) => block.text).join("\n");
  const complete = useModelScope ? choice?.finish_reason === "stop" : response.stop_reason === "end_turn";
  if (!code?.trim() || !complete || !response.id || !/minimax/i.test(response.model || "")) {
    throw new Error("The provider did not return completed source and a MiniMax response identifier.");
  }
  const source = code.trim().replace(/^\x60{3}(?:javascript|js)?\s*\n([\s\S]*?)\n\x60{3}$/i, "$1") + "\n";
  await writeFile(resolve(runRoot, "proposed-queue.mjs"), source);
  await writeFile(resolve(runRoot, "minimax-generation.json"), JSON.stringify({
    provider, endpoint: new URL(endpoint, host).href, requestedModel: model, model: response.model,
    requestId: response.id, usage: response.usage, prompt, context: pack, sourceSha256: hash(source),
  }, null, 2) + "\n");
  return remember("generated", {
    model: response.model, contextVersion: pack.contextVersion,
    sourceSha256: hash(source), next: "review and verify proposed-queue.mjs",
  });
}

function tests(cwd, files) {
  const run = spawnSync(process.execPath, ["--test", ...files], {
    cwd, env: { TZ: "UTC" }, encoding: "utf8", timeout: 30_000,
  });
  return { command: ["node", "--test", ...files], code: run.status, stdout: run.stdout, stderr: run.stderr };
}

async function verify() {
  const proposal = await readFile(resolve(runRoot, "proposed-queue.mjs"), "utf8");
  const generation = JSON.parse(await readFile(resolve(runRoot, "minimax-generation.json"), "utf8"));
  if (generation.sourceSha256 !== hash(proposal)) throw new Error("Proposed source differs from the MiniMax output.");
  const original = new Map(generation.context.sources.map(({ path, body }) => [path, body]));
  for (const [, path] of records) {
    if (original.has(path) && original.get(path) !== await readFile(new URL(path, root), "utf8")) {
      throw new Error(`Source ${path} changed after generation. Reindex before generating again.`);
    }
  }
  const candidate = resolve(runRoot, "candidate");
  await mkdir(resolve(candidate, "src"), { recursive: true });
  await mkdir(resolve(candidate, "test"), { recursive: true });
  for (const [category, path] of records) {
    if (category === "test") await writeFile(resolve(candidate, path), await readFile(new URL(path, root)));
  }
  await writeFile(resolve(candidate, target), proposal);
  const after = tests(candidate, ["test/baseline.test.mjs", "test/due.test.mjs"]);
  await writeFile(resolve(runRoot, "verification.json"), JSON.stringify(after, null, 2) + "\n");
  if (after.code !== 0) process.exitCode = 1;
  return remember(after.code === 0 ? "verified" : "needs-fix", {
    contextVersion: generation.context.contextVersion, sourceSha256: hash(proposal), exitCode: after.code,
    next: after.code === 0 ? "review evidence and submit" : "inspect verification.json",
  });
}

const commands = {
  index, status, context, generate, verify,
  async baseline() {
    await mkdir(runRoot, { recursive: true });
    const result = tests(root, ["test/baseline.test.mjs"]);
    const red = tests(root, ["test/due.test.mjs"]);
    await writeFile(resolve(runRoot, "baseline.json"), JSON.stringify({ result, red }, null, 2) + "\n");
    if (result.code !== 0 || red.code !== 1) throw new Error("Expected baseline regression to pass and new feature test to fail.");
    return remember("baseline", { regressions: "pass", dueFeature: "missing", next: "run the MiniMax generation" });
  },
};

if (process.send) {
  try {
    const command = process.argv[2];
    if (!Object.hasOwn(commands, command)) throw new Error("Choose index, baseline, context, generate, verify, or status.");
    const result = await commands[command]();
    process.send({ result, status: process.exitCode || 0 });
  } catch (error) {
    process.send({ error: error.message, status: 1 });
  }
} else {
  // The embedded library can keep native threads alive after database work has finished.
  const worker = fork(fileURLToPath(import.meta.url), process.argv.slice(2));
  let replied = false;
  worker.once("message", ({ result, error, status }) => {
    replied = true;
    if (error) console.error(error);
    else console.log(JSON.stringify(result, null, 2));
    process.exitCode = status;
    worker.kill();
  });
  worker.once("exit", () => {
    if (replied) return;
    console.error("The SDD worker stopped before returning a result.");
    process.exitCode = 1;
  });
}
