import assert from "node:assert/strict";
import fs from "node:fs/promises";

let source = await fs.readFile(new URL("./worker.js", import.meta.url), "utf8");
const bundledWatchlist = JSON.parse(await fs.readFile(new URL("./watchlist.json", import.meta.url), "utf8"));
source = source.replace(
  'import BUNDLED_WATCHLIST from "./watchlist.json";',
  `const BUNDLED_WATCHLIST = ${JSON.stringify(bundledWatchlist)};`,
);
source = source.replace("export default {", "const workerDefault = {");
source += `\nexport { cmdAdd, cmdRemove, cmdFind, cmdList, cmdInfo, cmdPriority, cmdSpecial,
  cmdSpecialList, cmdNationwide, cmdFlight, cmdAirport, cmdAircraftSearch,
  cmdMenu, adminPanel, adminModal, menuModal, menuModalToCommand, handleAdminButton,
  handleMenuButton, safeHandleMenuButton,
  handlePersonalAlertButton, handleLegacyPersonalAlertButton,
  handlePersonalPanelButton, handlePersonalPanelAction, personalModalSubmit,
  handleFeedbackButton, feedbackModalSubmit, processAircraftRequestSubmit,
  processAircraftRequestDecision,
  processAirportAllButton, airportAircraftMatches, airportAircraftDisplayName,
  watchlistAircraftMatches, lookupLiveryName, lifecycleButtonSpec, processLifecycleButton,
  withTimeout, directListResponse, processListFilterModal, handleListPageInteraction,
  WORKER_BUILD };`;
const moduleUrl = `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`;
const worker = await import(moduleUrl);

await assert.rejects(
  worker.withTimeout(new Promise(() => {}), 5, "test timeout"),
  /test timeout/,
);

const filterButtonResponse = await worker.handleMenuButton({
  data: { custom_id: "menu|list-filter" },
}, {}, {});
const filterButtonPayload = await filterButtonResponse.json();
assert.equal(filterButtonPayload.type, 9);
assert.equal(filterButtonPayload.data.custom_id, "menu_modal|list-filter");

// メニューの入力系ボタンは、外部APIを待たずDiscordへ有効なモーダルを即返す。
// 行数・入力欄数もDiscordの制限内であることを全ボタンについて保証する。
for (const action of ["aircraft-search", "info", "flight", "airport", "list-filter"]) {
  const response = await worker.handleMenuButton({
    data: { custom_id: `menu|${action}` },
  }, {}, {});
  const payload = await response.json();
  assert.equal(payload.type, 9, `${action} must return a modal response`);
  assert.equal(payload.data.custom_id, `menu_modal|${action}`);
  assert.ok(payload.data.title.length <= 45, `${action} modal title is too long`);
  assert.ok(payload.data.components.length >= 1 && payload.data.components.length <= 5);
  const inputIds = new Set();
  for (const row of payload.data.components) {
    assert.equal(row.type, 1);
    assert.equal(row.components.length, 1);
    const input = row.components[0];
    assert.equal(input.type, 4);
    assert.ok(input.custom_id.length <= 100);
    assert.ok(!inputIds.has(input.custom_id), `${action} has a duplicate input id`);
    inputIds.add(input.custom_id);
  }
}

const originalConsoleError = console.error;
let failedMenuResponse;
try {
  console.error = () => {};
  failedMenuResponse = await worker.safeHandleMenuButton(
    { data: { custom_id: "menu|aircraft-search" } },
    {},
    {},
    async () => { throw new Error("simulated menu failure"); },
  );
} finally {
  console.error = originalConsoleError;
}
const failedMenuPayload = await failedMenuResponse.json();
assert.equal(failedMenuPayload.type, 4);
assert.equal(failedMenuPayload.data.flags, 64);
assert.match(failedMenuPayload.data.content, /ボタンの応答に失敗/);

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
let adminReviewPayloads = [];
let dmPayloads = [];
let pendingAdminMessages = [];
const liveries = {
  JA339J: { name: "JAL Jubilee Express", valid_until: "2027-04-30" },
  JA784A: { name: "Test Special Livery", valid_until: "2027-12-31" },
};

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
  if (value.includes("/contents/liveries.json") && (init.method || "GET") === "GET") {
    return new Response(JSON.stringify({
      content: Buffer.from(JSON.stringify(liveries)).toString("base64"),
      sha: "livery-sha",
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (value.includes("/contents/liveries.json") && init.method === "PUT") {
    const body = JSON.parse(init.body);
    const updated = JSON.parse(Buffer.from(body.content, "base64").toString("utf8"));
    for (const key of Object.keys(liveries)) delete liveries[key];
    Object.assign(liveries, updated);
    return new Response("{}", { status: 200 });
  }
  if (value.includes("hexdb.io/reg-hex?")) {
    registrationLookupUrl = value;
    if (value.endsWith("reg=JA738A")) return new Response("n/a", { status: 404 });
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
  if (value.includes("hexdb.io/api/v1/aircraft/86804e")) {
    return new Response("n/a", { status: 404 });
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
  if (value.includes("api.adsbdb.com/v0/aircraft/86804e")) {
    return new Response(JSON.stringify({ response: "unknown aircraft" }), {
      status: 404, headers: { "content-type": "application/json" },
    });
  }
  if (value.includes("aerodatabox.p.rapidapi.com/aircrafts/reg/JA738A")) {
    return new Response(JSON.stringify({
      reg: "JA738A", hexIcao: "86804E", model: "Boeing 737 MAX 8",
      modelCode: "B38M", airlineName: "Skymark Airlines", firstFlightDate: "2026-03-20",
    }), { status: 200, headers: { "content-type": "application/json" } });
  }
  if (value.includes("aerodatabox.p.rapidapi.com/aircrafts/icao24/86804e")) {
    return new Response(JSON.stringify({
      reg: "JA738A", hexIcao: "86804E", model: "Boeing 737 MAX 8",
      modelCode: "B38M", airlineName: "Skymark Airlines", firstFlightDate: "2026-03-20",
    }), { status: 200, headers: { "content-type": "application/json" } });
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
  if (value.endsWith("discord.com/api/v10/channels/admin-channel/messages?limit=100")) {
    return new Response(JSON.stringify(pendingAdminMessages), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.endsWith("discord.com/api/v10/channels/admin-channel/messages")) {
    adminReviewPayloads.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ id: "review-message" }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.endsWith("discord.com/api/v10/users/@me/channels")) {
    return new Response(JSON.stringify({ id: "dm-channel" }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  if (value.endsWith("discord.com/api/v10/channels/dm-channel/messages")) {
    dmPayloads.push(JSON.parse(init.body));
    return new Response(JSON.stringify({ id: "dm-message" }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }
  throw new Error(`Unexpected fetch: ${value}`);
};

const requestButtonResponse = worker.handleFeedbackButton({
  data: { custom_id: "feedback:aircraft-request" },
});
const requestButtonPayload = await requestButtonResponse.json();
assert.equal(requestButtonPayload.type, 9);
assert.equal(requestButtonPayload.data.custom_id, "feedback_modal|aircraft-request");
assert.equal(requestButtonPayload.data.components[0].components[0].custom_id, "registration");

const requestEnv = {
  ...env,
  DISCORD_BOT_TOKEN: "discord-test-token",
  FEEDBACK_SOURCE_CHANNEL_ID: "source-channel",
  FEEDBACK_DESTINATION_CHANNEL_ID: "admin-channel",
};
const requestInteraction = {
  application_id: "app",
  token: "request-token",
  channel_id: "source-channel",
  member: { user: { id: "123456789012345678", username: "requester" } },
  data: {
    custom_id: "feedback_modal|aircraft-request",
    components: [{ components: [{ value: "g-ezbz" }] }],
  },
};
const requestTasks = [];
editedOriginalPayload = null;
const requestResponse = worker.feedbackModalSubmit(requestInteraction, requestEnv, {
  waitUntil(promise) { requestTasks.push(promise); },
});
const requestResponsePayload = await requestResponse.json();
assert.equal(requestResponsePayload.type, 5);
assert.equal(requestResponsePayload.data.flags, 64);
await Promise.all(requestTasks);
assert.match(editedOriginalPayload.content, /追加要望を管理者へ送りました/);
assert.equal(adminReviewPayloads.length, 1);
assert.equal(adminReviewPayloads[0].embeds[0].title, "🛩️ サーバー監視機体の追加要望");
assert.match(JSON.stringify(adminReviewPayloads[0]), /G-EZBZ/);
assert.match(JSON.stringify(adminReviewPayloads[0]), /Airbus A319 111/);
assert.match(JSON.stringify(adminReviewPayloads[0].components), /aircraft_request\|approve/);

pendingAdminMessages = [{ components: adminReviewPayloads[0].components }];
editedOriginalPayload = null;
const duplicateTasks = [];
worker.feedbackModalSubmit(requestInteraction, requestEnv, {
  waitUntil(promise) { duplicateTasks.push(promise); },
});
await Promise.all(duplicateTasks);
assert.equal(adminReviewPayloads.length, 1);
assert.match(editedOriginalPayload.content, /既に管理者の確認待ち/);
pendingAdminMessages = [];

editedOriginalPayload = null;
const newAircraftTasks = [];
worker.feedbackModalSubmit({
  ...requestInteraction,
  token: "new-aircraft-token",
  data: {
    custom_id: "feedback_modal|aircraft-request",
    components: [{ components: [{ value: "JA738A" }] }],
  },
}, { ...requestEnv, AERODATABOX_RAPIDAPI_KEY: "test" }, {
  waitUntil(promise) { newAircraftTasks.push(promise); },
});
await Promise.all(newAircraftTasks);
assert.match(editedOriginalPayload.content, /`JA738A` \(86804e\)/);
assert.equal(adminReviewPayloads.length, 2);
assert.match(JSON.stringify(adminReviewPayloads[1]), /Boeing 737 MAX 8/);
assert.match(JSON.stringify(adminReviewPayloads[1]), /Skymark Airlines/);

editedOriginalPayload = null;
dmPayloads = [];
await worker.processAircraftRequestDecision({
  application_id: "app",
  token: "decision-token",
  data: { custom_id: "aircraft_request|approve|123456789012345678|4010ee|G-EZBZ" },
  message: {
    embeds: adminReviewPayloads[0].embeds,
    components: adminReviewPayloads[0].components,
  },
}, requestEnv);
assert.equal(watchlist["4010ee"].label, "G-EZBZ");
assert.match(editedOriginalPayload.content, /サーバー監視へ追加しました/);
assert.deepEqual(editedOriginalPayload.components, []);
assert.equal(dmPayloads.length, 1);
assert.match(dmPayloads[0].content, /追加されました/);
delete watchlist["4010ee"];

let result = await worker.cmdAdd(
  { tail: "JA0001", icao24: "abc123", type: "TEST" }, env, interaction,
);
assert.match(result.content, /追加しました/);
assert.equal(watchlist.abc123.label, "JA0001");

result = await worker.cmdFind({ keyword: "JA0001" }, env);
assert.match(result.content, /JA0001/);
result = await worker.cmdList({ page: 1 }, env);
assert.match(result.content, /JA0001/);

Object.assign(watchlist, {
  b77w01: { label: "ER-TEST", type: "Boeing 777-300ER" },
  b77301: { label: "NONER-TEST", type: "Boeing 777-300" },
  a35901: { label: "A350-TEST", type: "Airbus A350-941" },
  a34001: { label: "A340-300-TEST", type: "Airbus A340 313E" },
  a34002: { label: "A340-600-TEST", type: "Airbus A340 642" },
});
result = await worker.cmdList({ page: 1, aircraft: "77W" }, env);
assert.match(result.content, /機種絞り込み：`77W`/);
assert.match(result.content, /`ER-TEST`/);
assert.doesNotMatch(result.content, /`NONER-TEST`/);
assert.match(JSON.stringify(result.components), /listpage\|1\|77W/);
assert.match(JSON.stringify(result.components), /listselect\|77W/);

result = await worker.cmdList({ page: 1, aircraft: "B773" }, env);
assert.match(result.content, /`NONER-TEST`/);
assert.doesNotMatch(result.content, /`ER-TEST`/);
result = await worker.cmdList({ page: 1, aircraft: "359" }, env);
assert.match(result.content, /A350-TEST/);
result = await worker.cmdList({ page: 1, aircraft: "Boeing 777-300ER" }, env);
assert.match(result.content, /ER-TEST/);
result = await worker.cmdList({ page: 1, aircraft: "a340" }, env);
assert.match(result.content, /A340-300-TEST/);
assert.match(result.content, /A340-600-TEST/);
assert.doesNotMatch(result.content, /A350-TEST/);

// 系列名は、説明名だけでなくICAO/IATAコードだけの登録にも同じように一致する。
for (const [query, type] of [
  ["A220", "BCS3"],
  ["A330", "A339"],
  ["A340", "346"],
  ["A350", "A35K"],
  ["B747", "B748"],
  ["B777", "77W"],
  ["B787", "789"],
  ["CRJ", "CRJ9"],
  ["DHC8", "DH4"],
  ["ATR72", "AT76"],
]) {
  assert.equal(
    worker.watchlistAircraftMatches({ type }, query),
    true,
    `${query} should include ${type}`,
  );
}
// ICAOコードと系列名が同じ場合は、短いコードを規格どおりの型式として扱う。
assert.equal(worker.watchlistAircraftMatches({ type: "Airbus A320neo" }, "A320"), false);
assert.equal(worker.watchlistAircraftMatches({ type: "Airbus A320neo" }, "Airbus A320"), true);
assert.equal(worker.watchlistAircraftMatches({ type: "Boeing 737-800" }, "B737"), false);
assert.equal(worker.watchlistAircraftMatches({ type: "Boeing 737-800" }, "Boeing 737"), true);
result = await worker.cmdList({ page: 1, aircraft: "ZZZZ" }, env);
assert.match(result.content, /一致する登録機はありません/);
delete watchlist.b77w01;
delete watchlist.b77301;
delete watchlist.a35901;
delete watchlist.a34001;
delete watchlist.a34002;

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

const aircraftAliasCases = [
  ["Boeing 777-300", ["773", "B773"]],
  ["Boeing 777-300ER", ["77W", "B77W"]],
  ["Boeing 777 381", ["773"]],
  ["Airbus A350-941", ["359", "A359", "A350"]],
  ["Airbus A350-1000", ["35K", "A35K", "351"]],
  ["Boeing 787-9", ["789", "B789"]],
  ["Boeing 737-846", ["738", "B738"]],
  ["Boeing 737 MAX 8", ["B38M", "38M", "7M8"]],
  ["Airbus A321neo", ["21N", "A21N", "32Q"]],
  ["Airbus A330-343", ["333", "A333"]],
  ["Bombardier Dash 8 Q400 / DHC-8-400", ["DH8D", "DH4", "Q400"]],
  ["Embraer 190", ["E190", "E90"]],
  ["ATR 72-600", ["AT76", "AT7"]],
  ["Airbus A220-300", ["BCS3", "223"]],
];
for (const [model, aliases] of aircraftAliasCases) {
  for (const alias of aliases) {
    assert.equal(worker.airportAircraftMatches({ aircraft: { model } }, null, alias), true, `${alias} should match ${model}`);
    assert.equal(worker.airportAircraftMatches({ aircraft: { model } }, null, alias.toLowerCase()), true, `${alias} should be case-insensitive`);
  }
}
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Boeing 777-300" } }, null, "77W"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Boeing 777-300ER" } }, null, "773"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Boeing 777-300ER" } }, null, "B773"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Boeing 777-381ER" } }, null, "773"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Boeing 777-381ER" } }, null, "77W"), true);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Airbus A321neo" } }, null, "321"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Airbus A321-200" } }, null, "32Q"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { model: "Boeing 737 MAX 8" } }, null, "738"), false);
assert.equal(worker.airportAircraftMatches({ aircraft: { icaoTypeCode: "E190" } }, null, "E190"), true);
assert.equal(worker.airportAircraftMatches({ aircraft: { iataTypeCode: "E90" } }, null, "E90"), true);
assert.equal(worker.lookupLiveryName(liveries, "ja-339j", "2026-10-11"), "JAL Jubilee Express");
assert.equal(worker.lookupLiveryName(liveries, "JA339J", "2027-05-01"), null);
assert.equal(worker.lookupLiveryName(liveries, "JA000A", "2026-10-11"), null);
assert.equal(
  worker.airportAircraftDisplayName({ aircraft: { model: "Boeing 777" } }),
  "Boeing 777（派生型不明）",
);
assert.equal(
  worker.airportAircraftDisplayName({ aircraft: { model: "Boeing 777", iataTypeCode: "77W" } }),
  "Boeing 777-300ER",
);
assert.equal(
  worker.airportAircraftDisplayName({
    aircraft: { model: "Boeing 777-200" },
    arrival: { aircraft: { model: "Boeing 777" } },
  }, { aircraft: { model: "Boeing 777" } }),
  "Boeing 777-200",
);
const genericDisplayCases = [
  [{ model: "Airbus A350" }, "Airbus A350（派生型不明）"],
  [{ model: "Airbus A350", icaoTypeCode: "A359" }, "Airbus A350-900"],
  [{ model: "Boeing 787", iataTypeCode: "789" }, "Boeing 787-9"],
  [{ model: "Boeing 737", icaoTypeCode: "B38M" }, "Boeing 737 MAX 8"],
  [{ model: "Airbus A320", iataTypeCode: "32N" }, "Airbus A320neo"],
  [{ model: "Airbus A330", icaoTypeCode: "A339" }, "Airbus A330-900neo"],
  [{ model: "ATR 72", icaoTypeCode: "AT76" }, "ATR 72-600"],
  [{ model: "DHC-8", iataTypeCode: "DH4" }, "DHC-8-400 / Q400"],
  [{ model: "Bombardier CRJ", icaoTypeCode: "CRJ9" }, "Bombardier CRJ900"],
];
for (const [aircraft, expected] of genericDisplayCases) {
  assert.equal(worker.airportAircraftDisplayName({ aircraft }), expected);
}
assert.equal(
  worker.airportAircraftDisplayName({ aircraft: { icaoTypeCode: "A35K" } }),
  "Airbus A350-1000",
);
assert.equal(
  worker.airportAircraftDisplayName({ aircraft: { model: "Boeing 777", icaoTypeCode: "A359" } }),
  "Boeing 777（派生型不明）",
);
assert.equal(
  worker.airportAircraftDisplayName(
    { aircraft: { model: "Airbus A350-941" } },
    { aircraft: { model: "Airbus A350" } },
  ),
  "Airbus A350-941",
);

airportSchedule = {
  airport: { iata: "HND", icao: "RJTT", name: "Haneda" },
  arrivals: [
    { number: "NH773", aircraft: { model: "Boeing 777-300" }, arrival: { scheduledTime: { local: "2026-10-11T10:00" } }, departure: { airport: { iata: "CTS" } } },
    { number: "NH77W", aircraft: { model: "Boeing 777-300ER" }, arrival: { scheduledTime: { local: "2026-10-11T10:10" } }, departure: { airport: { iata: "ITM" } } },
    { number: "JL359", aircraft: { model: "Airbus A350-941" }, arrival: { scheduledTime: { local: "2026-10-11T10:20" } }, departure: { airport: { iata: "FUK" } } },
  ],
  departures: [],
};
result = await worker.cmdAirport({ airport: "HND", aircraft: "77W" }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(result.content, /NH77W/);
assert.doesNotMatch(result.content, /NH773/);
assert.doesNotMatch(result.content, /JL359/);
result = await worker.cmdAirport({ airport: "HND", aircraft: "773" }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(result.content, /NH773/);
assert.doesNotMatch(result.content, /NH77W/);
assert.doesNotMatch(result.content, /JL359/);
result = await worker.cmdAirport({ airport: "HND", aircraft: "359" }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(result.content, /JL359/);
assert.doesNotMatch(result.content, /NH773|NH77W/);

airportSchedule = {
  airport: { iata: "NRT", icao: "RJAA", name: "Narita" },
  arrivals: [
    { number: "OS25", aircraft: { model: "Boeing 777", reg: "JA339J" }, arrival: { scheduledTime: { local: "2026-10-11T08:50" } }, departure: { airport: { iata: "VIE" } } },
    { number: "TK50", aircraft: { model: "Boeing 777", iataTypeCode: "77W" }, arrival: { scheduledTime: { local: "2026-10-11T08:55" } }, departure: { airport: { iata: "IST" } } },
  ],
  departures: [],
};
result = await worker.cmdAirport({ airport: "NRT", aircraft: "777" }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.match(result.content, /OS25.*Boeing 777（派生型不明）/);
assert.doesNotMatch(result.content, /🎨/);
assert.match(result.content, /TK50.*Boeing 777-300ER/);
result = await worker.cmdAirport({ airport: "NRT", aircraft: "77W" }, { ...env, AERODATABOX_RAPIDAPI_KEY: "test" });
assert.doesNotMatch(result.content, /OS25/);
assert.match(result.content, /TK50/);

airportSchedule = {
  airport: { iata: "HND", icao: "RJTT", name: "Haneda" },
  arrivals: Array.from({ length: 14 }, (_, index) => ({
    number: `NH${100 + index}`,
    status: ["Expected", "EnRoute", "Approaching", "Arrived", "CanceledUncertain", "Expected", "Delayed", "Delayed", "Diverted"][index] || "Unknown",
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
      ...(index === 6 ? { revisedTime: { utc: new Date(Date.now() - 40 * 60 * 1000).toISOString() } } : {}),
      ...(index === 7 ? { revisedTime: { utc: new Date(Date.now() + 30 * 60 * 1000).toISOString() } } : {}),
    },
  })),
  departures: [],
};
airportSchedule.arrivals[0].arrival.scheduledTime.utc = new Date(Date.now() + 30 * 60 * 1000).toISOString();
airportSchedule.arrivals[5].arrival.scheduledTime.utc = new Date(Date.now() + 90 * 60 * 1000).toISOString();
airportSchedule.arrivals[6].arrival.revisedTime = { utc: new Date(Date.now() + 40 * 60 * 1000).toISOString() };
airportSchedule.arrivals[7].arrival.revisedTime = { utc: new Date(Date.now() + 90 * 60 * 1000).toISOString() };
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
assert.match(result.content, /🟢 `09:12` \*\*NH106\*\*/);
assert.match(result.content, /🟠 `09:14` \*\*NH107\*\*/);
assert.match(result.content, /🟣 `09:16` \*\*NH108\*\*/);
assert.match(result.content, /🟠遅延/);
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
assert.doesNotMatch(JSON.stringify(result.embeds), /Test Special Livery/);

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
assert.match(JSON.stringify(menu.components), /空港の発着予定/);
assert.match(JSON.stringify(menu.components), /登録機を機種で検索/);
assert.doesNotMatch(JSON.stringify(menu.components), /日付を指定して発着を見る/);
assert.doesNotMatch(JSON.stringify(menu.components), /航空会社で発着を絞る/);
assert.match(worker.adminPanel().content, /管理画面/);
assert.match(JSON.stringify(worker.adminPanel().components), /watchlist同期/);
const syncStatusResponse = worker.handleAdminButton({
  member: { permissions: "8", user: { id: "123456789" } },
  data: { custom_id: "admin|watchlist-status" },
}, {}, {});
const syncStatusPayload = await syncStatusResponse.json();
assert.equal(syncStatusPayload.type, 4);
assert.equal(
  syncStatusPayload.data.content,
  "__WATCHLIST_STATUS__|123456789\n⏳ watchlistの同期状態を確認しています。結果は管理者DMへ送ります。",
);
assert.equal(worker.adminModal("priority").custom_id, "admin_modal|priority");
assert.equal(worker.menuModal("list-filter").custom_id, "menu_modal|list-filter");
const listFilterCommand = worker.menuModalToCommand({
  data: {
    components: [{ components: [{ custom_id: "aircraft", value: "77W" }] }],
  },
}, "list-filter");
assert.equal(listFilterCommand.data.name, "list");
assert.deepEqual(listFilterCommand.data.options, [{ name: "aircraft", value: "77W" }]);
assert.equal(worker.WORKER_BUILD, "2026-10-31-interaction-endpoint-recovery-v16");
Object.assign(watchlist, {
  a34001: { label: "A340-300-TEST", type: "Airbus A340 313E" },
  a34002: { label: "A340-600-TEST", type: "Airbus A340 642" },
});
editedOriginalPayload = null;
await worker.processListFilterModal({
  application_id: "app",
  token: "token",
  data: { options: [{ name: "aircraft", value: "A340" }] },
}, { ...env, GITHUB_REPO: "example/direct" }, null);
assert.match(editedOriginalPayload.content, /機種絞り込み：`A340`/);
assert.doesNotMatch(editedOriginalPayload.content, /A350/);
assert.match(JSON.stringify(editedOriginalPayload.components), /listpage\|1\|A340/);
assert.match(JSON.stringify(editedOriginalPayload.components), /listselect\|A340/);
delete watchlist.a34001;
delete watchlist.a34002;

const registeredCommands = JSON.parse(await fs.readFile(new URL("./worker/commands.json", import.meta.url), "utf8"));
const listCommand = registeredCommands.find((command) => command.name === "list");
assert.ok(listCommand.options.some((option) => option.name === "aircraft"));

let response = worker.handlePersonalAlertButton({
  id: "1420070400000000000",
  user: { id: "123" },
  data: { custom_id: "personal_alert|stop|123|87c003|" },
  message: { embeds: [], components: [] },
});
let responseBody = await response.json();
assert.match(responseBody.data.content, /変更を受け付けました/);
assert.match(responseBody.data.content, /__PERSONAL_ALERT__\|123\|alert_stop\|1420070400000000000\|87c003/);

response = worker.handlePersonalAlertButton({
  id: "1420070400000000004",
  user: { id: "123" },
  data: { custom_id: "personal_alert|stop|123|87c003||kyushu" },
  message: { embeds: [], components: [] },
});
responseBody = await response.json();
assert.match(
  responseBody.data.content,
  /__PERSONAL_ALERT__\|123\|alert_stop\|1420070400000000004\|87c003\|\|kyushu/,
);

response = worker.handlePersonalPanelButton({
  user: { id: "123" },
  data: { custom_id: "personal_panel:region" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /地方限定で機体を監視/);
assert.match(JSON.stringify(responseBody.data.components), /personal_action\|region_add/);
assert.match(JSON.stringify(responseBody.data.components), /personal_action\|region_list/);
assert.match(JSON.stringify(responseBody.data.components), /personal_action\|region_remove/);
assert.match(JSON.stringify(responseBody.data.components), /personal_action\|region_toggle/);

response = worker.handlePersonalPanelButton({
  user: { id: "123" },
  data: { custom_id: "personal_panel:flight_route" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /便・目的地の通知/);
assert.match(JSON.stringify(responseBody.data.components), /personal_panel:destination/);
assert.match(JSON.stringify(responseBody.data.components), /personal_panel:equipment/);

response = worker.handlePersonalPanelButton({
  user: { id: "123" },
  data: { custom_id: "personal_panel:destination" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /登録機の行き先を監視/);
assert.match(JSON.stringify(responseBody.data.components), /登録機＋到着空港を追加/);
assert.match(JSON.stringify(responseBody.data.components), /personal_action\|destination_toggle/);

response = worker.handlePersonalPanelButton({
  user: { id: "123" },
  data: { custom_id: "personal_panel:settings" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.doesNotMatch(JSON.stringify(responseBody.data.components), /settings_toggle/);
assert.match(responseBody.data.content, /機能ごと/);

response = worker.handlePersonalPanelButton({
  user: { id: "123" },
  data: { custom_id: "personal_panel:history" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /__PERSONAL_PANEL__\|123\|history/);

response = worker.handlePersonalPanelButton({
  user: { id: "123" },
  data: { custom_id: "personal_panel:history" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /__PERSONAL_PANEL__\|123\|history/);

response = worker.handlePersonalPanelAction({
  user: { id: "123" },
  data: { custom_id: "personal_action|region_add" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.equal(responseBody.type, 9);
assert.equal(responseBody.data.custom_id, "personal_modal|region_add");

response = worker.personalModalSubmit({
  user: { id: "123" },
  data: {
    custom_id: "personal_modal|region_add",
    components: [
      { components: [{ custom_id: "aircraft", value: "JA738B" }] },
      { components: [{ custom_id: "region", value: "九州" }] },
    ],
  },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.match(responseBody.data.content, /__PERSONAL_PANEL__\|123\|region_add\|JA738B\|九州/);

response = worker.handlePersonalPanelAction({
  user: { id: "123" },
  data: { custom_id: "personal_action|aircraft_toggle" },
  message: { id: "panel-message" },
});
responseBody = await response.json();
assert.equal(responseBody.type, 4);
assert.match(responseBody.data.content, /__PERSONAL_PANEL__\|123\|aircraft_toggle/);

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

let lifecycleSpec = worker.lifecycleButtonSpec({
  user: { id: "999" }, guild_id: null,
  data: { custom_id: "lifecycle|livery|remove|123|JA339J" },
});
assert.equal(lifecycleSpec.ok, false);
lifecycleSpec = worker.lifecycleButtonSpec({
  user: { id: "123" }, guild_id: null,
  data: { custom_id: "lifecycle|livery|remove|123|JA-339J" },
});
assert.deepEqual(lifecycleSpec, {
  ok: true, kind: "livery", action: "remove", ownerId: "123", target: "JA339J",
});
editedOriginalPayload = null;
airportFollowupPayloads = [];
await worker.processLifecycleButton({
  id: "1420070400000000020",
  application_id: "app",
  token: "token",
  user: { id: "123" },
  data: { custom_id: "lifecycle|livery|remove|123|JA-339J" },
  message: { components: [] },
}, env, lifecycleSpec);
assert.equal(liveries.JA339J, undefined);
assert.match(editedOriginalPayload.content, /塗装情報だけを削除/);
assert.match(airportFollowupPayloads[0].content, /__LIFECYCLE__\|123\|livery\|remove\|JA339J/);

result = await worker.cmdRemove({ target: "JA0001" }, env);
assert.match(result.content, /削除しました/);
assert.equal(Object.keys(watchlist).length, 0);

console.log("All server command contract tests passed.");
