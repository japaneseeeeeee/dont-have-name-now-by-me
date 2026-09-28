import assert from "node:assert/strict";
import fs from "node:fs/promises";

let source = await fs.readFile(new URL("./worker.js", import.meta.url), "utf8");
source = source.replace("export default {", "const workerDefault = {");
source += `\nexport { cmdAdd, cmdRemove, cmdFind, cmdList, cmdInfo, cmdPriority, cmdSpecial,
  cmdSpecialList, cmdNationwide, cmdFlight, cmdAirport, cmdAircraftSearch,
  cmdMenu, adminPanel, adminModal, menuModalToCommand };`;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const worker = await import(moduleUrl);

const env = { GITHUB_REPO: "example/repo", GITHUB_TOKEN: "test", GITHUB_BRANCH: "main" };
const interaction = { channel_id: "123" };
let watchlist = {};

globalThis.fetch = async (url, init = {}) => {
  const value = String(url);
  if (value.includes("/contents/watchlist.json") && (init.method || "GET") === "GET") {
    return new Response(JSON.stringify({
      content: Buffer.from(JSON.stringify(watchlist)).toString("base64"),
      sha: "test-sha",
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (value.includes("/contents/watchlist.json") && init.method === "PUT") {
    const body = JSON.parse(init.body);
    watchlist = JSON.parse(Buffer.from(body.content, "base64").toString("utf8"));
    return new Response("{}", { status: 200 });
  }
  if (value.endsWith("/dispatches")) return new Response(null, { status: 204 });
  throw new Error(`Unexpected fetch: ${value}`);
};

let result = await worker.cmdAdd(
  { tail: "JA0001", icao24: "abc123", type: "TEST" }, env, interaction,
);
assert.match(result.content, /追加しました/);
assert.equal(watchlist.abc123.label, "JA0001");

result = await worker.cmdFind({ keyword: "JA0001" }, env);
assert.match(result.content, /JA0001/);
result = await worker.cmdList({ page: 1 }, env);
assert.match(result.content, /JA0001/);

result = await worker.cmdPriority({ aircraft: "JA0001", level: "WATCH" }, env);
assert.match(result.content, /WATCH/);
assert.equal(watchlist.abc123.priority, "WATCH");

result = await worker.cmdSpecial({ aircraft: "JA0001", duration: "24h" }, env);
assert.match(result.content, /SPECIAL/);
assert.equal(watchlist.abc123.priority, "SPECIAL");
result = await worker.cmdSpecialList(env);
assert.match(result.content, /JA0001/);

result = await worker.cmdNationwide({ aircraft: "JA0001", enabled: true }, env);
assert.match(result.content, /ON/);
assert.equal(watchlist.abc123.nationwide_alert, true);

result = await worker.cmdAircraftSearch({ query: "B77W", airline: "ANA" }, env, interaction);
assert.match(result.content, /詳しく検索/);
result = await worker.cmdFlight({ query: "JL12" }, env, interaction);
assert.match(result.content, /詳しく検索/);
result = await worker.cmdAirport({ airport: "XX" }, env);
assert.match(result.content, /空港は3文字/);
result = await worker.cmdInfo({ aircraft: "" }, env);
assert.match(result.content, /使い方/);

assert.match(worker.cmdMenu().content, /航空機通知Botメニュー/);
assert.match(worker.adminPanel().content, /管理画面/);
assert.equal(worker.adminModal("priority").custom_id, "admin_modal|priority");

result = await worker.cmdRemove({ target: "JA0001" }, env);
assert.match(result.content, /削除しました/);
assert.equal(Object.keys(watchlist).length, 0);

console.log("All server command contract tests passed.");
