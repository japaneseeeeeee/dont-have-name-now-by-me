/**
 * aircraft-alert — Discord スラッシュコマンドの受け口(Cloudflare Workers、依存ライブラリなし)
 *
 * /add /remove /find /list /flight を、すぐ(数秒で)返信する。
 * - ウォッチリスト(watchlist.json)は、GitHubのリポジトリに保存する(Contents API)。
 * - すぐに見つからなかった /add /flight は、GitHub Actions(lookup.yml)に依頼して、
 *   結果をチャンネルに投稿してもらう(60万機の機体データベースは、Workersの無料枠では重すぎるため)。
 *
 * 必要な設定(Cloudflareの「変数とシークレット」):
 *   DISCORD_PUBLIC_KEY  (シークレット) DiscordアプリのPUBLIC KEY
 *   GITHUB_TOKEN        (シークレット) このリポジトリだけに権限がある、Contents: Read and write のトークン
 *   GITHUB_REPO         (テキスト)     例: yourname/aircraft-alert
 *   GITHUB_BRANCH       (テキスト、省略可) 既定は main
 *   ALLOWED_CHANNEL_ID  (テキスト、省略可) 指定すると、そのチャンネルでだけコマンドが使える
 */

const UA = "aircraft-alert-worker/1.0";
const DISCORD_API = "https://discord.com/api/v10";
// 受信局のカバー範囲や一時的な遅延で1つの提供元に出ない便があるため、
// 同じADS-B形式を返す複数の提供元を順に照会する。
const ADSB_API_BASES = [
  "https://api.adsb.lol",
];
const PAGE_SIZE = 20;
const HEX6 = /^[0-9a-fA-F]{6}$/;
const UNKNOWN_TYPE = "不明";
const COLOR_AIRBORNE = 0x3498db;
const COLOR_GROUND = 0x2ecc71;
const AIRPORT_SHORT_NAMES = {
  NRT: "Narita", HND: "Haneda", NGO: "Chubu", KIX: "Kansai",
  ITM: "Itami", CTS: "New Chitose", FUK: "Fukuoka", OKA: "Naha",
};

// IATA航空会社コード → ICAOコード(コールサインの先頭3文字)。足りない会社は追記してよい。
// (GitHub側の lib.py の IATA_TO_ICAO と同じ内容)
const IATA_TO_ICAO = {
  JL: "JAL", NH: "ANA", MM: "APJ", GK: "JJP", BC: "SKY", "7G": "SFJ",
  NU: "JTA", HD: "ADO", IJ: "SJO", "6J": "SNJ", FW: "IBX", OC: "ORC", "3X": "JAC",
  KE: "KAL", OZ: "AAR", "7C": "JJA", LJ: "JNA", TW: "TWB", BX: "ABL",
  CX: "CPA", UO: "HKE", HX: "CRK", CI: "CAL", BR: "EVA", IT: "TTW",
  CA: "CCA", MU: "CES", CZ: "CSN", HU: "CHH", "3U": "CSC",
  SQ: "SIA", TR: "TGW", TG: "THA", FD: "AIQ", VN: "HVN", VJ: "VJC",
  PR: "PAL", MH: "MAS", AK: "AXM", GA: "GIA", AI: "AIC",
  QF: "QFA", JQ: "JST", "3K": "JSA", NZ: "ANZ", VA: "VOZ",
  AA: "AAL", DL: "DAL", UA: "UAL", AC: "ACA", HA: "HAL", AS: "ASA",
  BA: "BAW", LH: "DLH", AF: "AFR", KL: "KLM", AY: "FIN", TK: "THY",
  EK: "UAE", QR: "QTR", EY: "ETD",
  FX: "FDX", "5X": "UPS", KZ: "NCA", CV: "CLX", "5Y": "GTI", K4: "CKS",
};

// ============ エントリポイント ============

export default {
  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("aircraft-alert worker is running.", { status: 200 });
    }

    const body = await request.text();
    if (!(await verifyDiscordRequest(request, body, env.DISCORD_PUBLIC_KEY))) {
      return new Response("invalid request signature", { status: 401 });
    }

    let interaction;
    try {
      interaction = JSON.parse(body);
    } catch {
      return new Response("bad request", { status: 400 });
    }

    // 1: PING(Discordが、エンドポイントURLの確認に送ってくる)
    if (interaction.type === 1) return json({ type: 1 });

    // 2: スラッシュコマンド
    if (interaction.type === 2) {
      const allowed = (env.ALLOWED_CHANNEL_ID || "").trim();
      if (allowed && interaction.channel_id !== allowed) {
        return json({ type: 4, data: { content: "このチャンネルでは使えません。", flags: 64 } });
      }
      if (["add", "remove", "priority", "special", "special-list", "nationwide"].includes(interaction.data?.name) && !isAdministrator(interaction)) {
        return json({
          type: 4,
          data: { content: "⛔ このコマンドはサーバー管理者だけが使用できます。", flags: 64 },
        });
      }
      // 3秒以内に返す必要があるので、まず「考え中…」を返し、結果はあとから書き換える
      ctx.waitUntil(processCommand(interaction, env));
      return json({ type: 5 });
    }

    return new Response("unsupported interaction", { status: 400 });
  },
};

function isAdministrator(interaction) {
  try {
    const permissions = BigInt(interaction.member?.permissions || "0");
    return (permissions & 8n) === 8n;
  } catch {
    return false;
  }
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json;charset=UTF-8" },
  });
}

// ============ Discordの署名検証 ============

function hexToBytes(hex) {
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i++) out[i] = parseInt(hex.slice(i * 2, i * 2 + 2), 16);
  return out;
}

async function verifyDiscordRequest(request, body, publicKeyHex) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  if (!signature || !timestamp || !publicKeyHex) return false;
  if (!/^[0-9a-fA-F]+$/.test(signature) || signature.length % 2 !== 0) return false;

  const data = new TextEncoder().encode(timestamp + body);
  const keyBytes = hexToBytes(publicKeyHex);
  const sigBytes = hexToBytes(signature);

  // Cloudflare Workers は標準の "Ed25519" と、従来の "NODE-ED25519" の両方に対応している
  for (const algo of [{ name: "Ed25519" }, { name: "NODE-ED25519", namedCurve: "NODE-ED25519" }]) {
    try {
      const key = await crypto.subtle.importKey("raw", keyBytes, algo, false, ["verify"]);
      return await crypto.subtle.verify(algo.name, key, sigBytes, data);
    } catch {
      // この名前に対応していない環境。次の名前を試す
    }
  }
  return false;
}

// ============ コマンドの振り分け ============

async function processCommand(interaction, env) {
  let message;
  try {
    message = await runCommand(interaction, env);
  } catch (err) {
    console.error("command failed:", (err && err.stack) || err);
    message = { content: "⚠️ 処理中にエラーが起きました。少し待ってから、もう一度試してください。" };
  }
  await editOriginal(interaction, message);
}

async function editOriginal(interaction, message) {
  const payload = { allowed_mentions: { parse: [] }, ...message };
  if (payload.content) payload.content = payload.content.slice(0, 2000);
  const url = `${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;
  const r = await fetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json", "user-agent": UA },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.error("editOriginal failed:", r.status, await r.text());
}

function optionsOf(interaction) {
  const out = {};
  for (const o of interaction.data.options || []) out[o.name] = o.value;
  return out;
}

async function runCommand(interaction, env) {
  const o = optionsOf(interaction);
  switch (interaction.data.name) {
    case "add": return cmdAdd(o, env, interaction);
    case "remove": return cmdRemove(o, env);
    case "find": return cmdFind(o, env);
    case "list": return cmdList(o, env);
    case "info": return cmdInfo(o, env);
    case "flight": return cmdFlight(o, env, interaction);
    case "priority": return cmdPriority(o, env);
    case "special": return cmdSpecial(o, env);
    case "special-list": return cmdSpecialList(env);
    case "nationwide": return cmdNationwide(o, env);
    default: return { content: "未対応のコマンドです。" };
  }
}

async function cmdSpecial(o, env) {
  const aircraft = String(o.aircraft || "").trim();
  const match = String(o.duration || "24h").trim().match(/^(\d+)(m|h|d)$/i);
  if (!aircraft || !match) return { content: "⚠️ 期間は `30m` / `24h` / `7d` の形式で指定してください。" };
  const seconds = Number(match[1]) * ({m:60,h:3600,d:86400}[match[2].toLowerCase()]);
  const result = await updateWatchlist(env, (wl) => {
    const id = wl[aircraft.toLowerCase()] !== undefined ? aircraft.toLowerCase() : Object.keys(wl).find((x) => normalize(wl[x]).label.toUpperCase() === aircraft.toUpperCase());
    if (!id) return { changed:false };
    const entry = normalize(wl[id]);
    wl[id] = {
      ...(typeof wl[id] === "object" ? wl[id] : {}),
      label: entry.label,
      type: entry.type,
      priority: "SPECIAL",
      priority_after_special: entry.priority || "NORMAL",
      special_until: Date.now() / 1000 + seconds,
    };
    return { changed:true, id, label:entry.label, until:Math.floor(Date.now()/1000 + seconds) };
  }, `watchlist: special ${aircraft}`);
  return result.changed ? { content:`🚨 \`${result.label}\` (${result.id}) を <t:${result.until}:F> まで **SPECIAL** に設定しました。` } : { content:`⚠️ \`${aircraft}\` はwatchlistに見つかりませんでした。` };
}

async function cmdSpecialList(env) {
  const { data } = await readWatchlist(env);
  const items = Object.entries(data).filter(([, value]) => String(value?.priority || "NORMAL").toUpperCase() === "SPECIAL");
  if (!items.length) return { content: "SPECIAL登録機はありません。" };
  return { content: `🚨 SPECIAL登録機\n${items.map(([id, value]) => `\`${normalize(value).label}\` (${id})`).join("\n")}`.slice(0, 1990) };
}

async function cmdPriority(o, env) {
  const aircraft = String(o.aircraft || "").trim();
  const level = String(o.level || "").trim().toUpperCase();
  if (!aircraft || !["NORMAL", "WATCH", "SPECIAL"].includes(level)) return { content: "⚠️ aircraft と level を指定してください。" };
  const result = await updateWatchlist(env, (wl) => {
    const key = aircraft.toLowerCase();
    const id = wl[key] !== undefined ? key : Object.keys(wl).find((x) => normalize(wl[x]).label.toUpperCase() === aircraft.toUpperCase());
    if (!id) return { changed: false };
    const entry = normalize(wl[id]);
    wl[id] = { ...(typeof wl[id] === "object" ? wl[id] : {}), label: entry.label, type: entry.type, priority: level };
    return { changed: true, id, label: entry.label };
  }, `watchlist: priority ${aircraft} ${level}`);
  return result.changed ? { content: `✅ \`${result.label}\` (${result.id}) を **${level}** に設定しました。` } : { content: `⚠️ \`${aircraft}\` はwatchlistに見つかりませんでした。` };
}

async function cmdNationwide(o, env) {
  const aircraft = String(o.aircraft || "").trim();
  const enabled = o.enabled === true;
  if (!aircraft || typeof o.enabled !== "boolean") {
    return { content: "⚠️ aircraft と enabled を指定してください。" };
  }
  const result = await updateWatchlist(env, (wl) => {
    const key = aircraft.toLowerCase();
    const id = wl[key] !== undefined ? key : Object.keys(wl).find((x) => normalize(wl[x]).label.toUpperCase() === aircraft.toUpperCase());
    if (!id) return { changed: false };
    const entry = normalize(wl[id]);
    const original = typeof wl[id] === "object" ? wl[id] : {};
    wl[id] = { ...original, label: entry.label, type: entry.type };
    if (enabled) wl[id].nationwide_alert = true;
    else delete wl[id].nationwide_alert;
    return { changed: true, id, label: entry.label };
  }, `watchlist: nationwide ${aircraft} ${enabled ? "on" : "off"}`);
  if (!result.changed) return { content: `⚠️ \`${aircraft}\` はwatchlistに見つかりませんでした。` };
  return { content: `🗾 \`${result.label}\` (${result.id}) の全国通知を **${enabled ? "ON" : "OFF"}** にしました。` };
}

// ============ コマンド ============

async function cmdAdd(o, env, interaction) {
  const tail = String(o.tail || "").trim().toUpperCase();
  let icao24 = o.icao24 ? String(o.icao24).trim() : null;
  let typeName = o.type ? String(o.type).trim() : null;
  if (!tail) return { content: "⚠️ 使い方: `/add tail:<登録記号>`" };

  // icao24の欄に機種名が書かれた場合(例: icao24=Boeing type=777)は、機種として扱う
  if (icao24 && !HEX6.test(icao24)) {
    typeName = typeName ? `${icao24} ${typeName}` : icao24;
    icao24 = null;
  }

  if (!icao24) {
    icao24 = await lookupIcao24(tail);
    if (!icao24) {
      // すぐには見つからない → GitHub Actionsで、60万機のデータベースを使って詳しく検索する
      return startSlowLookup(env, "add", [tail, ...(typeName ? [typeName] : [])], interaction, {
        failure: `⚠️ \`${tail}\` のicao24が自動取得できませんでした。` +
          `\`/add tail:${tail} icao24:<icao24>\` の形で手動指定してください。`,
      });
    }
  }
  icao24 = icao24.toLowerCase();
  if (!typeName) typeName = (await lookupAircraftType(icao24)) || UNKNOWN_TYPE;

  const outcome = await updateWatchlist(env, (wl) => {
    if (wl[icao24] !== undefined) return { changed: false, existing: normalize(wl[icao24]).label };
    wl[icao24] = { label: tail, type: typeName };
    return { changed: true };
  }, `watchlist: add ${tail}`);

  if (outcome.existing) return { content: `ℹ️ \`${outcome.existing}\` (${icao24}) は既に登録済みです。` };
  return { content: `✅ \`${tail}\` (${icao24} / ${typeName}) をwatchlistに追加しました。` };
}

async function cmdRemove(o, env) {
  const key = String(o.target || "").trim();
  if (!key) return { content: "⚠️ 使い方: `/remove target:<登録記号 または icao24>`" };

  const outcome = await updateWatchlist(env, (wl) => {
    const lower = key.toLowerCase();
    if (wl[lower] !== undefined) {
      const { label } = normalize(wl[lower]);
      delete wl[lower];
      return { changed: true, label, icao24: lower };
    }
    for (const [icao24, value] of Object.entries(wl)) {
      const { label } = normalize(value);
      if (label.toUpperCase() === key.toUpperCase()) {
        delete wl[icao24];
        return { changed: true, label, icao24 };
      }
    }
    return { changed: false };
  }, `watchlist: remove ${key}`);

  if (!outcome.changed) return { content: `⚠️ \`${key}\` はwatchlistに見つかりませんでした。` };
  return { content: `🗑️ \`${outcome.label}\` (${outcome.icao24}) をwatchlistから削除しました。` };
}

async function cmdFind(o, env) {
  const keyword = String(o.keyword || "").trim();
  if (!keyword) return { content: "⚠️ 使い方: `/find keyword:<キーワード>`" };
  const { data } = await readWatchlist(env);
  const upper = keyword.toUpperCase();
  const matches = [];
  for (const [icao24, value] of Object.entries(data)) {
    const { label, type } = normalize(value);
    if (label.toUpperCase().includes(upper) || icao24.toUpperCase().includes(upper)) {
      matches.push(`\`${label}\` (${icao24} / ${type})`);
    }
  }
  if (matches.length === 0) return { content: `「${keyword}」に一致する機体はありません。` };
  let text = matches.slice(0, PAGE_SIZE).join("\n");
  if (matches.length > PAGE_SIZE) text += `\n…他 ${matches.length - PAGE_SIZE} 件(キーワードを絞ってください)`;
  return { content: text };
}

async function cmdInfo(o, env) {
  const query = String(o.aircraft || "").trim().toUpperCase();
  if (!query) return { content: "⚠️ 使い方: `/info aircraft:<登録記号 / icao24>`" };

  const compact = query.replace(/[\s-]/g, "");
  const { data } = await readWatchlist(env);
  let icao24 = HEX6.test(compact) ? compact.toLowerCase() : null;
  let entry = icao24 ? data[icao24] : null;

  if (!entry) {
    const found = Object.entries(data).find(([id, value]) =>
      id.toUpperCase() === compact || normalize(value).label.toUpperCase() === query,
    );
    if (found) {
      [icao24, entry] = found;
    }
  }
  if (!icao24) icao24 = await lookupIcao24(query);
  if (!icao24) return { content: `❓ \`${query}\` の機体情報は見つかりませんでした。` };

  const normalized = entry ? normalize(entry) : { label: query, type: UNKNOWN_TYPE };
  const details = await lookupAircraftDetails(icao24);
  const type = normalized.type !== UNKNOWN_TYPE ? normalized.type : details.type || UNKNOWN_TYPE;
  const priority = String(entry?.priority || "NORMAL").toUpperCase();
  const year = details.year || "不明";
  const age = aircraftAge(details.year);
  return {
    content: `✈️ **機体情報**\n` +
      `登録記号: \`${normalized.label}\`\n` +
      `icao24: \`${icao24}\`\n` +
      `機種: ${type}\n` +
      `製造年: ${year}${age ? `（機齢 約${age}年）` : ""}\n` +
      `運航会社: ${details.operator || "不明"}\n` +
      `登録国: ${details.country || "不明"}\n` +
      `通知レベル: **${priority}**\n` +
      `全国通知: **${entry?.nationwide_alert === true ? "ON" : "OFF"}**` +
      (entry ? "\nwatchlist: 登録済み" : "\nwatchlist: 未登録"),
  };
}

async function cmdList(o, env) {
  const { data } = await readWatchlist(env);
  const items = Object.entries(data)
    .map(([icao24, value]) => ({ icao24, ...normalize(value) }))
    .sort((a, b) => a.label.toUpperCase().localeCompare(b.label.toUpperCase()));
  if (items.length === 0) return { content: "watchlistは空です。" };

  const pages = Math.ceil(items.length / PAGE_SIZE);
  const page = Math.max(1, Math.min(Number(o.page) || 1, pages));
  const lines = items
    .slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
    .map((x) => `\`${x.label}\` (${x.icao24} / ${x.type})`);
  const footer = page < pages ? `\n次のページ: \`/list page:${page + 1}\`` : "";
  return { content: `📋 watchlist 全${items.length}機(ページ ${page}/${pages})\n${lines.join("\n")}${footer}`.slice(0, 1990) };
}

async function cmdFlight(o, env, interaction) {
  const query = String(o.query || "").trim();
  if (!query) {
    return { content: "⚠️ 使い方: `/flight query:<便名 / コールサイン / 登録記号 / icao24>`" };
  }

  // /flight のリアルタイムADS-B検索はGitHub Actions側で実行する。
  // Cloudflareからadsb.lolへアクセスすると429になるため直接検索しない。
  return startSlowLookup(
    env,
    "flight",
    [query],
    interaction,
    { failure: notFoundText(query) },
  );
}


function notFoundText(query) {
  return `❓ 「${query}」に一致する機体は、いまのADS-Bでは見つかりませんでした。` +
    "離陸前・着陸後、受信範囲外、または便名とコールサインが違う便の可能性があります" +
    "（便名・コールサイン・登録記号・icao24で検索できます）。";
}

// 「JL123」「JAL123」のような便名・コールサインでなければ、登録記号とみなす
function looksLikeRegistration(query) {
  const t = query.toUpperCase().replace(/[\s-]/g, "");
  if (t.length < 4) return false;
  if (/^[A-Z0-9]{2}\d{1,4}[A-Z]?$/.test(t)) return false; // IATA便名(JL123)
  if (/^[A-Z]{3}\d{1,4}[A-Z]?$/.test(t)) return false;    // コールサイン(JAL123)
  if (HEX6.test(t)) return false;                          // icao24
  return true;
}

// ============ 詳しい検索(GitHub Actionsに依頼) ============

async function startSlowLookup(env, op, args, interaction, { failure }) {
  const r = await gh(env, "/dispatches", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ event_type: "lookup", client_payload: { op, args, channel_id: interaction.channel_id } }),
  });
  if (r.status !== 204) {
    console.error("dispatch failed:", r.status, await r.text());
    return { content: failure };
  }
  return {
    content: "🔎 すぐには見つからなかったため、詳しく検索しています(1分ほどかかります)。" +
      "結果は、このチャンネルにあらためて送ります。",
  };
}

// ============ ウォッチリスト(GitHub Contents API) ============

function branchOf(env) {
  return (env.GITHUB_BRANCH || "main").trim();
}

function gh(env, path, init = {}) {
  return fetch(`https://api.github.com/repos/${env.GITHUB_REPO}${path}`, {
    ...init,
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": UA,
      ...(init.headers || {}),
    },
  });
}

function utf8ToB64(str) {
  let bin = "";
  for (const b of new TextEncoder().encode(str)) bin += String.fromCharCode(b);
  return btoa(bin);
}

function b64ToUtf8(b64) {
  const bin = atob(b64.replace(/\s/g, ""));
  return new TextDecoder().decode(Uint8Array.from(bin, (c) => c.charCodeAt(0)));
}

// Python側の json.dump(sort_keys=True, indent=2) と同じ並び・形式にそろえる(差分が出ないように)
function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map((k) => [k, sortDeep(value[k])]));
  }
  return value;
}

async function readWatchlist(env) {
  const r = await gh(env, `/contents/watchlist.json?ref=${encodeURIComponent(branchOf(env))}`);
  if (r.status === 404) return { data: {}, sha: null };
  if (!r.ok) throw new Error(`GitHub read failed: ${r.status} ${await r.text()}`);
  const file = await r.json();
  const parsed = JSON.parse(b64ToUtf8(file.content) || "{}");
  return { data: parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}, sha: file.sha };
}

// mutate(data) が { changed: true } を返したときだけ書き込む。他と衝突したら読み直して再試行する
async function updateWatchlist(env, mutate, message) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const { data, sha } = await readWatchlist(env);
    const outcome = mutate(data);
    if (!outcome.changed) return outcome;

    const r = await gh(env, "/contents/watchlist.json", {
      method: "PUT",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        message,
        content: utf8ToB64(JSON.stringify(sortDeep(data), null, 2) + "\n"),
        branch: branchOf(env),
        ...(sha ? { sha } : {}),
      }),
    });
    if (r.ok) return outcome;
    if (r.status === 409 || r.status === 422) continue; // 読んでから書くまでの間に、別の更新が入った
    throw new Error(`GitHub write failed: ${r.status} ${await r.text()}`);
  }
  throw new Error("GitHub write conflict (3回試して失敗)");
}

function normalize(value) {
  if (value && typeof value === "object") {
    return { label: value.label || "?", type: value.type || UNKNOWN_TYPE };
  }
  return { label: String(value), type: UNKNOWN_TYPE };
}

// ============ 機体情報の検索 ============

// Discordの応答期限内に必ず返すため、外部APIの待機時間は短くする。
async function fetchJson(url, timeoutMs = 4000) {
  try {
    const r = await fetch(url, {
      headers: { "user-agent": UA },
      signal: AbortSignal.timeout(timeoutMs),
    });
    if (!r.ok) {
      console.error("fetchJson HTTP error:", r.status, url);
      return null;
    }
    return await r.json();
  } catch (err) {
    console.error("fetchJson failed:", url, err?.name, err?.message);
    return null;
  }
}

async function lookupIcao24(registration) {
  try {
    const r = await fetch(`https://hexdb.io/api/v1/aircraft/reg-icao/${encodeURIComponent(registration)}`, {
      headers: { "user-agent": UA },
      signal: AbortSignal.timeout(1200),
    });
    if (!r.ok) return null;
    const text = (await r.text()).trim();
    return HEX6.test(text) ? text.toLowerCase() : null;
  } catch {
    return null;
  }
}

async function lookupAircraftType(icao24) {
  const data = await fetchJson(`https://hexdb.io/api/v1/aircraft/${icao24}`, 1200);
  if (!data) return null;
  const maker = data.Manufacturer || "";
  const type = data.Type || data.ICAOTypeCode || "";
  return `${maker} ${type}`.trim() || null;
}

async function lookupAircraftDetails(icao24) {
  // HexDBは機種と所有者には強いが、登録国を返さない機体が多い。
  // ADSBDBも並行して参照し、片方にしかない項目を補完する。
  const [hexData, adsbData] = await Promise.all([
    fetchJson(`https://hexdb.io/api/v1/aircraft/${icao24}`, 1800),
    fetchJson(`https://api.adsbdb.com/v0/aircraft/${icao24}`, 1800),
  ]);
  const data = hexData || {};
  const adsb = adsbData?.response?.aircraft || {};
  return {
    type: (
      `${data.Manufacturer || ""} ${data.Type || data.ICAOTypeCode || ""}`.trim()
      || `${adsb.manufacturer || ""} ${adsb.type || adsb.icao_type || ""}`.trim()
      || null
    ),
    year: String(data.Year || data.YearOfManufacture || data.FirstRegistered || "").match(/^\d{4}/)?.[0] || null,
    operator: (
      data.RegisteredOwners
      || data.RegisteredOwnerOperatorName
      || data.RegisteredOwnerOperator
      || data.RegisteredOwner
      || adsb.registered_owner
      || null
    ),
    country: (
      data.RegisteredOwnerCountry
      || data.RegisteredOwnerNationality
      || adsb.registered_owner_country_name
      || adsb.registered_owner_country_iso_name
      || null
    ),
  };
}

function aircraftAge(year) {
  const value = Number(year);
  const currentYear = new Date().getUTCFullYear();
  return Number.isInteger(value) && value > 1900 && value <= currentYear ? currentYear - value : null;
}

function callsignCandidates(text) {
  const t = text.toUpperCase().replace(/[\s-]/g, "");
  const out = [];
  const m = /^([A-Z0-9]{2})(\d{1,4}[A-Z]?)$/.exec(t);
  if (m && IATA_TO_ICAO[m[1]]) out.push(IATA_TO_ICAO[m[1]] + m[2]);
  out.push(t);
  return [...new Set(out.filter(Boolean))];
}

async function adsbLookup(kind, value) {
  const results = await Promise.all(
    ADSB_API_BASES.map((base) => fetchJson(`${base}/v2/${kind}/${encodeURIComponent(value)}`)),
  );
  const data = results.find((item) => item && Array.isArray(item.ac) && item.ac.length > 0);
  return data ? data.ac : [];
}

async function findLive(text) {
  for (const callsign of callsignCandidates(text)) {
    const found = await adsbLookup("callsign", callsign);
    if (found.length > 0) return found;
  }
  const compact = text.replace(/[\s-]/g, "");
  if (HEX6.test(compact)) {
    const found = await adsbLookup("hex", compact.toLowerCase());
    if (found.length > 0) return found;
  }
  const hex = await lookupIcao24(text.trim().toUpperCase());
  if (hex) {
    const found = await adsbLookup("hex", hex);
    if (found.length > 0) return found;
  }
  return [];
}

async function fetchRoute(callsign) {
  const cs = (callsign || "").trim();
  if (!cs) return null;
  const data = await fetchJson(`https://api.adsbdb.com/v0/callsign/${encodeURIComponent(cs)}`, 1200);
  const fr = data && data.response && typeof data.response === "object" ? data.response.flightroute : null;
  if (!fr || !fr.origin || !fr.destination) return null;
  return { origin: fr.origin, destination: fr.destination, flightIata: fr.callsign_iata };
}

// ============ Embed ============

const COMPASS = ["北", "北東", "東", "南東", "南", "南西", "西", "北西"];
const compass = (deg) => COMPASS[Math.floor((deg + 22.5) / 45) % 8];
const num = (v) => typeof v === "number" && Number.isFinite(v);
const fmt0 = (v) => Math.round(v).toLocaleString("en-US");

function formatAirport(a) {
  const iata = String(a.iata_code || "").toUpperCase();
  const code = iata || a.icao_code || "?";
  const name = AIRPORT_SHORT_NAMES[iata] || a.name || a.municipality || "";
  return name ? `${name} (${code})` : code;
}

function buildFlightEmbed(ac, route) {
  const callsign = (ac.flight || "").trim();
  const hex = (ac.hex || "").toLowerCase();
  const reg = ac.r;
  const alt = ac.alt_baro;
  const onGround = alt === "ground";

  const fields = [
    { name: "機種", value: ac.desc || ac.t || "不明", inline: true },
    { name: "登録記号", value: reg ? `\`${reg}\`` : "不明", inline: true },
    { name: "icao24", value: `\`${hex}\``, inline: true },
  ];
  if (route) {
    const flight = route.flightIata ? `${route.flightIata} · ` : "";
    fields.push({
      name: "区間(予定)",
      value: `${flight}${formatAirport(route.origin)} → ${formatAirport(route.destination)}`,
      inline: false,
    });
  }
  if (!onGround) {
    if (num(alt)) fields.push({ name: "高度", value: `${fmt0(alt)} ft (${fmt0(alt * 0.3048)} m)`, inline: true });
    if (num(ac.gs)) fields.push({ name: "速度", value: `${fmt0(ac.gs * 1.852)} km/h (${fmt0(ac.gs)} kt)`, inline: true });
    if (num(ac.track)) fields.push({ name: "進行方向", value: `${compass(ac.track)} (${Math.round(ac.track)}°)`, inline: true });
    if (num(ac.baro_rate) && Math.abs(ac.baro_rate) >= 64) {
      const arrow = ac.baro_rate > 0 ? "↑ 上昇" : "↓ 降下";
      fields.push({ name: "垂直速度", value: `${arrow} ${fmt0(Math.abs(ac.baro_rate))} ft/min`, inline: true });
    }
  }
  if (num(ac.lat) && num(ac.lon)) {
    fields.push({ name: "位置", value: `${ac.lat.toFixed(3)}, ${ac.lon.toFixed(3)}`, inline: true });
  } else {
    fields.push({ name: "位置", value: "位置情報なし", inline: true });
  }
  if (num(ac.seen_pos) && ac.seen_pos > 60) {
    fields.push({ name: "最終位置受信", value: `${Math.round(ac.seen_pos)}秒前`, inline: true });
  }

  let footer = "ADS-B: adsb.lol / adsb.one · タイトルをタップで地図";
  if (reg) footer += ` · 監視に追加: /add tail:${reg}`;
  return {
    title: `✈️ ${callsign || hex}${reg ? ` (${reg})` : ""}`,
    url: `https://globe.adsbexchange.com/?icao=${hex}`,
    description: onGround ? "**地上**" : "**飛行中**",
    color: onGround ? COLOR_GROUND : COLOR_AIRBORNE,
    fields,
    footer: { text: footer },
  };
}
