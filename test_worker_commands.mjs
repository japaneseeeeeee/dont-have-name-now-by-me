import assert from "node:assert/strict";
import fs from "node:fs/promises";

let source = await fs.readFile(new URL("./worker.js", import.meta.url), "utf8");
source = source.replace("export default {", "const workerDefault = {");
source += `\nexport { cmdAdd, cmdRemove, cmdFind, cmdList, cmdInfo, cmdPriority, cmdSpecial,
  cmdSpecialList, cmdNationwide, cmdFlight, cmdAirport, cmdAircraftSearch,
  cmdMenu, adminPanel, adminModal, menuModalToCommand,
  handlePersonalAlertButton, handleLegacyPersonalAlertButton,
  processAirportAllButton };`;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const worker = await import(moduleUrl);

const env = { GITHUB_REPO: "example/repo", GITHUB_TOKEN: "test", GITHUB_BRANCH: "main" };
const interaction = { channel_id: "123" };
let watchlist = {};
let airportRequestUrl = "";
let airportSchedule = { airport: { iata: "HND", icao: "RJTT", name: "Haneda" }, arrivals: [], departures: [] };
let dispatchStatus = 204;
let dispatchBody = null;
let registrationLookupUrl = "";
let registrationLookupStatus = 200;
let aeroDataBoxAircraftLookup = false;
let editedOriginalPayload = null;
let airportFollowupPayloads = [];

globalThis.fetch = async (url, init = {}) => {
  const value = String(url);
  if (value.includes("aerodatabox.p.rapidapi.com/flights/airports/")) {
    airportRequestUrl = value;
    return new Response(JSON.stringify(airportSchedule), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }
  if (value.includes("raw.githubusercontent.com/") && value.endsWith("/watchlist.json")) {
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } });
  }
  if (value.includes("hexdb.io/reg-hex?")) {
    registrationLookupUrl = value;
    return new Response(registrationLookupStatus === 200 ? "4010EE" : "", { status: registrationLookupStatus });
  }
  if (value.includes("hexdb.io/api/v1/aircraft/4010ee")) {
    return new Response(JSON.stringify({
      Registration: "G-EZBZ", Manufacturer: "Airbus", Type: "A319 111",
      RegisteredOwners: "easyJet Airline", Year: "2008",
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (value.includes("hexdb.io/api/v1/aircraft/8691aa")) {
    return new Response(JSON.stringify({
      Registration: "JA784A", Manufacturer: "Boeing", Type: "777 381ER",
      RegisteredOwners: "All Nippon Airways",
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (value.includes("api.adsbdb.com/v0/aircraft/4010ee")) {
    return new Response(JSON.stringify({ response: { aircraft: {} } }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.includes("api.adsbdb.com/v0/aircraft/8691aa")) {
    return new Response(JSON.stringify({ response: { aircraft: { registration: "JA784A", registered_owner_country_name: "Japan" } } }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.includes("aerodatabox.p.rapidapi.com/aircrafts/reg/JA784A")) {
    aeroDataBoxAircraftLookup = true;
    return new Response(JSON.stringify({ reg: "JA784A", firstFlightDate: "2010-08-25" }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.includes("api.adsb.lol/v2/hex/4010ee")) {
    return new Response(JSON.stringify({ ac: [] }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.includes("api.adsb.lol/v2/hex/8691aa")) {
    return new Response(JSON.stringify({ ac: [] }), {
      status: 200, headers: { "content-type": "application/json" },
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
  if (value.endsWith("/dispatches")) {
    dispatchBody = JSON.parse(init.body);
    return new Response(null, { status: dispatchStatus });
  }
  if (value.includes("discord.com/api/v10/webhooks/") && value.endsWith("/messages/@original")) {
    editedOriginalPayload = JSON.parse(init.body);
    return new Response(null, { status: 200 });
  }
  if (value.includes("discord.com/api/v10/webhooks/")) {
    airportFollowupPayloads.push(JSON.parse(init.body));
    return new Response(null, { status: 200 });
  }
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
result = await worker.cmdAirport({ airport: "HND" }, env);
assert.match(result.content, /現在一時的に利用できません/);
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

airportSchedule = {
  airport: { iata: "HND", icao: "RJTT", name: "Haneda" },
  arrivals: Array.from({ length: 14 }, (_, index) => ({
    number: `NH${100 + index}`,
    status: ["Expected", "EnRoute", "Approaching", "Arrived", "CanceledUncertain", "Expected"][index] || "Unknown",
    airline: { name: "All Nippon Airways", iata: "NH", icao: "ANA" },
    arrival: index === 1
      ? { scheduledTimeLocal: "2026-10-10 09:10:00" }
      : {
          scheduledTime: { local: `2026-10-10T09:${String(index * 2).padStart(2, "0")}` },
          ...(index === 0 ? { revisedTime: { local: "2026-10-10T09:15" } } : {}),
        },
    departure: {
      airport: { iata: "CTS" },
      ...(index === 0 ? { scheduledTime: { utc: new Date(Date.now() - 30 * 60 * 1000).toISOString() } } : {}),
      ...(index === 5 ? { scheduledTime: { utc: new Date(Date.now() + 30 * 60 * 1000).toISOString() } } : {}),
    },
  })),
  departures: [],
};
airportSchedule.arrivals[0].arrival.scheduledTime.utc = new Date(Date.now() + 30 * 60 * 1000).toISOString();
airportSchedule.arrivals[5].arrival.scheduledTime.utc = new Date(Date.now() + 90 * 60 * 1000).toISOString();
result = await worker.cmdAirport(
  { airport: "HND", hours: 3 },
  { ...env, AERODATABOX_RAPIDAPI_KEY: "test" },
);
assert.match(result.content, /`09:00→09:15`/);
assert.match(result.content, /`09:10`/);
assert.match(result.content, /🟢 `09:00→09:15` \*\*NH100\*\*/);
assert.match(result.content, /🟢 `09:10` \*\*NH101\*\*/);
assert.match(result.content, /🟢 `09:04` \*\*NH102\*\*/);
assert.match(result.content, /🔵 `09:06` \*\*NH103\*\*/);
assert.match(result.content, /🔴 `09:08` \*\*NH104\*\*/);
assert.match(result.content, /🟡 `09:10` \*\*NH105\*\*/);
assert.match(result.content, /下のボタンで全便表示/);
assert.match(JSON.stringify(result.components), /全便を表示（14便）/);
assert.match(JSON.stringify(result.components), /airportall\|HND\|both\|3\|-\|-/);
assert.ok(result.components[0].components[0].custom_id.length <= 100);

result = await worker.cmdAirport(
  { airport: "HND", hours: 3, show_all: true },
  { ...env, AERODATABOX_RAPIDAPI_KEY: "test" },
);
assert.match(result.content, /NH113/);
assert.deepEqual(result.components, []);

editedOriginalPayload = null;
airportFollowupPayloads = [];
await worker.processAirportAllButton({
  application_id: "app",
  token: "token",
  data: { custom_id: "airportall|HND|both|3|-|-" },
  message: { content: "絞り込み: 航空会社: ANA" },
}, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(editedOriginalPayload.content, /NH113/);
assert.deepEqual(editedOriginalPayload.components, []);
assert.ok(airportFollowupPayloads.every((payload) => payload.flags === 64));

result = await worker.cmdInfo({ aircraft: "" }, env);
assert.match(result.content, /使い方/);
result = await worker.cmdInfo({ aircraft: "gezbz" }, env, interaction);
assert.match(registrationLookupUrl, /hexdb\.io\/reg-hex\?reg=G-EZBZ$/);
assert.equal(result.embeds[0].title, "✈️ G-EZBZ");
assert.match(JSON.stringify(result.embeds), /ICAO24: `4010ee`/);
assert.match(JSON.stringify(result.embeds), /未登録/);
assert.match(JSON.stringify(result.embeds), /Airbus A319 111/);
assert.match(JSON.stringify(result.embeds), /機齢 約18年/);
assert.match(JSON.stringify(result.components), /現在位置を更新/);
assert.match(JSON.stringify(result.components), /地図を見る/);
assert.equal(Object.keys(watchlist).length, 1);

result = await worker.cmdInfo(
  { aircraft: "8691aa" },
  { ...env, AERODATABOX_RAPIDAPI_KEY: "test" },
  interaction,
);
assert.equal(aeroDataBoxAircraftLookup, true);
assert.equal(result.embeds[0].title, "✈️ JA784A");
assert.match(JSON.stringify(result.embeds), /2010（初飛行年）/);
assert.match(JSON.stringify(result.embeds), /機齢 約16年/);

watchlist["4010ee"] = { label: "G-EZBZ", type: "Airbus A319-111" };
registrationLookupUrl = "";
result = await worker.cmdInfo({ aircraft: "g-ezbz" }, env, interaction);
assert.equal(result.embeds[0].title, "✈️ G-EZBZ");
assert.equal(registrationLookupUrl, "");
assert.match(JSON.stringify(result.embeds), /登録済み/);
delete watchlist["4010ee"];

registrationLookupStatus = 404;
result = await worker.cmdInfo({ aircraft: "ezb" }, env, interaction);
assert.match(result.content, /詳しく検索/);
assert.equal(dispatchBody.client_payload.op, "info_search");
assert.deepEqual(dispatchBody.client_payload.args, ["EZB", 0]);
registrationLookupStatus = 200;

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
assert.match(responseBody.data.content, /\|24h/);

for (const [duration, id] of [["1h", "1420070400000000010"], ["6h", "1420070400000000011"], ["morning", "1420070400000000012"], ["24h", "1420070400000000013"]]) {
  response = worker.handlePersonalAlertButton({
    id,
    user: { id: "123" },
    data: { custom_id: `personal_alert|mute|123|${duration}` },
    message: { embeds: [], components: [] },
  });
  responseBody = await response.json();
  assert.match(responseBody.data.content, new RegExp(`__PERSONAL_ALERT__\\|123\\|alert_mute\\|${id}\\|${duration}`));
}

dispatchStatus = 503;
result = await worker.cmdFlight({ query: "JL12" }, env, interaction);
assert.match(result.content, /現在一時的に利用できません/);
dispatchStatus = 204;

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
