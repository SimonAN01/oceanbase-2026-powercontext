import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import assert from "node:assert/strict";
import { test } from "node:test";

async function start(database) {
  const child = spawn(process.execPath, ["server.mjs"], {
    env: { ...process.env, HOST: "127.0.0.1", PORT: "0", GAME_DB: database },
    stdio: ["ignore", "pipe", "pipe"],
  });
  let stderr = "";
  child.stderr.on("data", (chunk) => { stderr += chunk; });
  const exited = once(child, "exit");
  let url;
  try {
    await Promise.race([
      (async () => {
        for await (const line of child.stdout) {
          const match = line.toString().match(/ready on port (\d+)/);
          if (match) {
            url = `http://127.0.0.1:${match[1]}/turn`;
            return;
          }
        }
        assert.fail(`Game exited before opening its port. ${stderr}`);
      })(),
      exited.then(() => assert.fail(`Game could not start. ${stderr}`)),
    ]);
  } catch (error) {
    child.kill();
    throw error;
  }
  return {
    async turn(action, ticket = "validation-case") {
      const response = await fetch(url, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ ticket, action }),
        signal: AbortSignal.timeout(5_000),
      });
      assert.equal(response.status, 200);
      return response.text();
    },
    async stop() {
      child.kill();
      await exited;
    },
  };
}

test("two parallel investigations survive restart, while another dossier starts empty", { timeout: 60_000 }, async () => {
  const database = await mkdtemp(join(tmpdir(), "lunar-game-"));
  let game;
  try {
    game = await start(database);
    assert.match(await game.turn("调查：温室"), /请先/);
    assert.match(await game.turn("开始或继续"), /线索 0\/3/);
    assert.match(await game.turn("指认：冰柜机"), /至少调查两处/);
    const [greenhouse, workshop] = await Promise.all([game.turn("调查：温室"), game.turn("调查：维修间")]);
    assert.match(greenhouse, /三角形磁封/);
    assert.match(workshop, /轮子拆下清洁/);
    assert.match(await game.turn("指认：运货机"), /线索 2\/3 \| 误判 1/);
    await game.stop();
    game = await start(database);
    const resumed = await game.turn("开始或继续");
    assert.match(resumed, /线索 2\/3 \| 误判 1/);
    assert.match(resumed, /温室 · 19:04/);
    assert.match(resumed, /维修间 · 维修台/);
    const closed = await game.turn("指认：冰柜机");
    assert.match(closed, /已结案/);
    assert.match(closed, /冻伤发生前找回了它/);
    assert.match(await game.turn("开始或继续"), /已结案/);
    assert.match(await game.turn("开始或继续", "separate-case"), /线索 0\/3 \| 误判 0/);
    console.log("Confirmed parallel clue writes, wrong-guess count, restart, resolution, and separate dossiers.");
  } finally {
    if (game) await game.stop();
    await rm(database, { recursive: true, force: true });
  }
});
