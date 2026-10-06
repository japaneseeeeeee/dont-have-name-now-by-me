import assert from "node:assert/strict";
import fs from "node:fs/promises";

let source = await fs.readFile(new URL("./worker.js", import.meta.url), "utf8");
source = source.replace("export default {", "const workerDefault = {");
source += `\nexport { cmdAdd, cmdRemove, cmdFind, cmdList, cmdInfo, cmdPriority, cmdSpecial,
  cmdSpecialList, cmdNationwide, cmdFlight, cmdAirport, cmdAircraftSearch,
  cmdMenu, adminPanel, adminModal, menuModalToCommand,
  handlePersonalAlertButton, handleLegacyPersonalAlertButton };`;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const worker = await import(moduleUrl);

const env = { GITHUB_REPO: "example/repo", GITHUB_TOKEN: "test", GITHUB_BRANCH: "main" };
const interaction = { channel_id: "123" };
let watchlist = {};
let airportRequestUrl = "";
let airportSchedule = { airport: { iata: "HND", icao: "RJTT", name: "Haneda" }, arrivals: [], departures: [] };

globalThis.fetch = async (url, init = {}) => {
  const value = String(url);
  if (value.includes("aerodatabox.p.rapidapi.com/flights/airports/")) {
    airportRequestUrl = value;
    return new Response(JSON.stringify(airportSchedule), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }
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
result = await worker.cmdAirport({ airport: "HND", date: "2026-02-30" }, env);
assert.match(result.content, /実在する日付/);
result = await worker.cmdAirport({ airport: "HND", start_time: "15:00" }, env);
assert.match(result.content, /date/);
result = await worker.cmdAirport({ airport: "HND", date: "2026-10-03", start_time: "15:00", hours: 6 }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(airportRequestUrl, /2026-10-03T15%3A00\/2026-10-03T21%3A00/);
assert.match(result.content, /2026-10-03 15:00から6時間/);
const airlineSchedule = {
  airport: { iata: "HND", icao: "RJTT", name: "Haneda" },
  arrivals: [
    { number: "NH10", airline: { name: "All Nippon Airways", iata: "NH", icao: "ANA" }, arrival: { scheduledTime: { local: "2026-10-03T10:00" } } },
    { number: "JL20", airline: { name: "Japan Airlines", iata: "JL", icao: "JAL" }, arrival: { scheduledTime: { local: "2026-10-03T10:10" } } },
  ],
  departures: [],
};
airportSchedule = airlineSchedule;
result = await worker.cmdAirport({ airport: "HND", airline: "ANA" }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(result.content, /NH10/);
assert.doesNotMatch(result.content, /JL20/);
assert.match(result.content, /航空会社: ANA/);
result = await worker.cmdInfo({ aircraft: "" }, env);
assert.match(result.content, /使い方/);

const menu = worker.cmdMenu();
assert.match(menu.content, /航空機通知Botメニュー/);
assert.match(JSON.stringify(menu.components), /現在の発着を見る/);
assert.doesNotMatch(JSON.stringify(menu.components), /日付を指定して発着を見る/);
assert.doesNotMatch(JSON.stringify(menu.components), /航空会社で発着を絞る/);
assert.match(worker.adminPanel().content, /管理画面/);
assert.equal(worker.adminModal("priority").custom_id, "admin_modal|priority");

let response = worker.handlePersonalAlertButton({
  id: "1420070400000000000",
  user: { id: "123" },
  data: { custom_id: "personal_alert|stop|123|87c003|" },
  message: { embeds: [], components: [] },
});
let responseBody = await response.json();
assert.match(responseBody.data.content, /変更を受け付けました/);
assert.match(responseBody.data.content, /__PERSONAL_ALERT__\|123\|alert_stop\|1420070400000000000\|87c003/);

response = worker.handleLegacyPersonalAlertButton({
  id: "1420070400000000001",
  user: { id: "123" },
  data: { custom_id: "old-random-id" },
  message: {
    embeds: [{ fields: [{ name: "ICAO24", value: "`87c003`" }] }],
    components: [{ components: [{ custom_id: "old-random-id", label: "この機体の通知を停止" }] }],
  },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /__PERSONAL_ALERT__\|123\|alert_stop\|1420070400000000001\|87c003/);

response = worker.handleLegacyPersonalAlertButton({
  id: "1420070400000000002",
  user: { id: "123" },
  data: { custom_id: "old-mute-id" },
  message: {
    embeds: [],
    components: [{ components: [{ custom_id: "old-mute-id", label: "個人通知を24時間休止" }] }],
  },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /__PERSONAL_ALERT__\|123\|alert_mute\|1420070400000000002/);

response = worker.handlePersonalAlertButton({
  id: "1420070400000000003",
  user: { id: "999" },
  data: { custom_id: "personal_alert|stop|123|87c003|" },
  message: { embeds: [], components: [] },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /登録者本人だけ/);

result = await worker.cmdRemove({ target: "JA0001" }, env);
assert.match(result.content, /削除しました/);
assert.equal(Object.keys(watchlist).length, 0);

console.log("All server command contract tests passed.");
