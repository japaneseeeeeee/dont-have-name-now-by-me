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
 *   ALLOWED_CHANNEL_ID  (テキスト、省略可) 指定すると、一般用と管理者用チャンネルでコマンドを使える
 */

const UA = "aircraft-alert-worker/1.0";
const DISCORD_API = "https://discord.com/api/v10";
const DEFAULT_GITHUB_REPO = "japaneseeeeeee/dont-have-name-now-by-me";
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
const TEMPORARILY_UNAVAILABLE =
  "⚠️ 現在一時的に利用できません。外部データサービスで障害または混雑が発生しています。少し待ってから再度お試しください。";
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
      if (allowed && interaction.channel_id !== allowed && interaction.channel_id !== "1554455009037516830") {
        return json({ type: 4, data: { content: "このチャンネルでは使えません。", flags: 64 } });
      }
      if (["add", "remove", "priority", "special", "special-list", "nationwide", "menu"].includes(interaction.data?.name) && !isAdministrator(interaction)) {
        return json({
          type: 4,
          data: { content: "⛔ このコマンドはサーバー管理者だけが使用できます。", flags: 64 },
        });
      }
      // 3秒以内に返す必要があるので、まず「考え中…」を返し、結果はあとから書き換える
      ctx.waitUntil(processCommand(interaction, env));
      // /airport はチャンネルを埋めないよう、実行者本人だけに見える応答にする。
      return json(interaction.data?.name === "airport"
        ? { type: 5, data: { flags: 64 } }
        : { type: 5 });
    }

    // 3: メッセージ内のボタン
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("menu|")) {
      return handleMenuButton(interaction, env, ctx);
    }

    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("feedback:")) {
      return handleFeedbackButton(interaction);
    }

    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("photo:")) {
      return handlePhotoButton(interaction);
    }

    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("equipment_server:")) {
      return handleEquipmentServerButton(interaction);
    }

    // 登録機一覧のページ移動（前へ・次へ・ページ選択）
    if (interaction.type === 3 && (
      interaction.data?.custom_id?.startsWith("listpage|") ||
      interaction.data?.custom_id === "listselect"
    )) {
      ctx.waitUntil(processListPageButton(interaction, env));
      return json({ type: 6 });
    }

    // 3: 管理画面内の操作ボタン
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("admin|")) {
      return handleAdminButton(interaction, env, ctx);
    }

    // 3: 機体検索結果の「次の5件」ボタン
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("searchpage|")) {
      ctx.waitUntil(processSearchPageButton(interaction, env));
      return json({ type: 4, data: { flags: 64, content: "🔎 次の5件を詳しく検索しています（1分ほどかかります）。" } });
    }

    // 3: 検索結果の「watchlistへ登録」ボタン
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("watchadd|")) {
      if (!isAdministrator(interaction)) {
        return json({
          type: 4,
          data: { content: "⛔ watchlistへの登録はサーバー管理者だけが実行できます。", flags: 64 },
        });
      }
      ctx.waitUntil(processWatchAddButton(interaction, env));
      return json({ type: 6 });
    }

    // 3: 本人専用チャンネルの個人設定パネル
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("personal_panel:")) {
      return handlePersonalPanelButton(interaction);
    }

    // 3: 個人設定パネル内の追加操作
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("personal_action|")) {
      return handlePersonalPanelAction(interaction);
    }

    // 個人通知DMに付けた停止・24時間休止ボタン。
    if (interaction.type === 3 && interaction.data?.custom_id?.startsWith("personal_alert|")) {
      return handlePersonalAlertButton(interaction);
    }

    // 修正前に送信済みの通知はdiscord.py生成のランダムcustom_idを持つため、
    // メッセージ内のボタン名と埋め込みから操作内容を復元する。
    if (interaction.type === 3) {
      const legacyAlertResponse = handleLegacyPersonalAlertButton(interaction);
      if (legacyAlertResponse) return legacyAlertResponse;
    }

    // 5: Botメニューから開いた入力画面
    if (interaction.type === 5 && interaction.data?.custom_id?.startsWith("menu_modal|")) {
      const command = String(interaction.data.custom_id).split("|")[1] || "";
      const synthetic = menuModalToCommand(interaction, command);
      ctx.waitUntil(processCommand(synthetic, env));
      return json({ type: 5, data: { flags: 64 } });
    }

    // 5: 管理画面から開いた入力画面
    if (interaction.type === 5 && interaction.data?.custom_id?.startsWith("admin_modal|")) {
      if (!isAdministrator(interaction)) {
        return json({ type: 4, data: { content: "⛔ 管理者だけが使用できます。", flags: 64 } });
      }
      const command = String(interaction.data.custom_id).split("|")[1] || "";
      const synthetic = menuModalToCommand(interaction, command);
      ctx.waitUntil(processCommand(synthetic, env));
      return json({ type: 5, data: { flags: 64 } });
    }

    // 5: 個人設定パネルの入力画面
    if (interaction.type === 5 && interaction.data?.custom_id?.startsWith("personal_modal|")) {
      return personalModalSubmit(interaction);
    }

    if (interaction.type === 5 && interaction.data?.custom_id?.startsWith("feedback_modal|")) {
      return feedbackModalSubmit(interaction);
    }

    if (interaction.type === 5 && interaction.data?.custom_id?.startsWith("equipment_server_modal|")) {
      return equipmentServerModalSubmit(interaction);
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

function feedbackModal(kind) {
  return {
    custom_id: `feedback_modal|${kind}`,
    title: kind === "question" ? "Botへの質問" : "Botの改善要望",
    components: [{ type: 1, components: [{
      type: 4, custom_id: "body", style: 2,
      label: kind === "question" ? "質問内容" : "改善してほしい内容",
      placeholder: "できるだけ詳しく入力してください", required: true,
      min_length: 3, max_length: 1500,
    }] }],
  };
}

function feedbackBridge(interaction, operation, body = "") {
  const userId = String(interaction.member?.user?.id || interaction.user?.id || "");
  const messageId = String(interaction.message?.id || "");
  return json({ type: 4, data: {
    content: `__FEEDBACK__|${userId}|${operation}|${encodeURIComponent(body)}|${messageId}`,
    allowed_mentions: { parse: [] },
  } });
}

function handleFeedbackButton(interaction) {
  const action = String(interaction.data?.custom_id || "").split(":")[1] || "";
  if (action === "question" || action === "request") {
    return json({ type: 9, data: feedbackModal(action) });
  }
  if (action === "help") {
    return json({ type: 4, data: { flags: 64, content:
      "❓ **質問**：Botの使い方や動作について管理者へ送信します。\n" +
      "💡 **改善要望**：追加してほしい機能や直してほしい点を送信します。\n" +
      "📖 **ボタンの説明**：この案内を表示します。\n" +
      "🔄 **最新位置へ移動**：管理者がパネルをチャンネルの一番下へ移動します。\n\n" +
      "回答はBotからDMで届くため、サーバーメンバーからのDMを許可してください。"
    } });
  }
  if (action === "refresh") {
    if (!isAdministrator(interaction)) {
      return json({ type: 4, data: { flags: 64, content: "⛔ パネルの移動は管理者だけが実行できます。" } });
    }
    return feedbackBridge(interaction, "refresh");
  }
  return json({ type: 4, data: { flags: 64, content: "⚠️ このボタンは利用できません。" } });
}

function feedbackModalSubmit(interaction) {
  const kind = String(interaction.data?.custom_id || "").split("|")[1] || "question";
  const body = String(interaction.data?.components?.[0]?.components?.[0]?.value || "").trim();
  return feedbackBridge(interaction, kind, body);
}

function channelPanelBridge(interaction, panel, operation, args = []) {
  const userId = String(interaction.member?.user?.id || interaction.user?.id || "");
  const messageId = String(interaction.message?.id || "");
  const values = args.map((value) => encodeURIComponent(String(value || "").trim()));
  while (values.length < 2) values.push("");
  return json({ type: 4, data: {
    content: ["__CHANNEL_PANEL__", userId, panel, operation, values[0], values[1], messageId].join("|"),
    allowed_mentions: { parse: [] },
  } });
}

function handlePhotoButton(interaction) {
  const action = String(interaction.data?.custom_id || "").split(":")[1] || "";
  if (action === "help") return json({ type: 4, data: { flags: 64, content:
    "📖 **ボタンの説明**\n" +
    "📖 **ボタンの説明・投稿方法**：この案内と写真の書き方を表示します。\n" +
    "🛩️ **機体情報を調べる**：登録記号またはICAO24から機体の詳細を調べます。\n" +
    "🔄 **最新位置へ移動**：管理者がパネルをチャンネルの一番下へ移動します。\n\n" +
    "📸 **投稿方法**\n写真を添付し、本文を次の順に書いてください。\n`JA784A`\n`成田空港`\n`夕方の光がきれいでした！`\n\nBotが機体情報付きのスレッドを自動作成します。"
  } });
  if (action === "refresh") {
    return isAdministrator(interaction) ? channelPanelBridge(interaction, "photo", "refresh")
      : json({ type: 4, data: { flags: 64, content: "⛔ パネルの移動は管理者だけが実行できます。" } });
  }
  return json({ type: 4, data: { flags: 64, content: "⚠️ このボタンは利用できません。" } });
}

function equipmentServerModal(action) {
  if (action === "add") return { custom_id: "equipment_server_modal|add", title: "サーバー機材通知を追加", components: [
    textInput("flight", "便名", "例: JL12", true, 12),
    textInput("equipment", "機材コード", "例: B77W", true, 12),
  ] };
  if (action === "remove") return { custom_id: "equipment_server_modal|remove", title: "サーバー機材通知を解除", components: [
    textInput("rule_id", "登録ID", "例: 1", true, 8),
  ] };
  return null;
}

function handleEquipmentServerButton(interaction) {
  if (!isAdministrator(interaction)) return json({ type: 4, data: { flags: 64, content: "⛔ 管理者だけが使用できます。" } });
  const action = String(interaction.data?.custom_id || "").split(":")[1] || "";
  if (action === "add" || action === "remove") return json({ type: 9, data: equipmentServerModal(action) });
  if (action === "list" || action === "refresh") return channelPanelBridge(interaction, "equipment", action);
  if (action === "help") return json({ type: 4, data: { flags: 64, content:
    "📖 **ボタンの説明**\n" +
    "➕ **機材投入通知を追加**：便名と機材コードを登録します。\n" +
    "📋 **登録便を見る**：現在登録されている通知条件を表示します。\n" +
    "🗑️ **機材投入通知を解除**：登録IDを指定して解除します。\n" +
    "📖 **ボタンの説明**：この案内を表示します。\n" +
    "🔄 **最新位置へ移動**：パネルをチャンネルの一番下へ移動します。\n\n" +
    "指定した便に指定機材が実際に投入されたことをADS-Bで確認すると、このチャンネルへ通知します。予定表ではなく飛行中の実機情報を約2分ごとに確認します。"
  } });
  return json({ type: 4, data: { flags: 64, content: "⚠️ このボタンは利用できません。" } });
}

function equipmentServerModalSubmit(interaction) {
  if (!isAdministrator(interaction)) return json({ type: 4, data: { flags: 64, content: "⛔ 管理者だけが使用できます。" } });
  const action = String(interaction.data?.custom_id || "").split("|")[1] || "";
  const values = {};
  for (const row of interaction.data?.components || []) {
    for (const field of row.components || []) values[field.custom_id] = String(field.value || "").trim();
  }
  return action === "add" ? channelPanelBridge(interaction, "equipment", "add", [values.flight, values.equipment])
    : action === "remove" ? channelPanelBridge(interaction, "equipment", "remove", [values.rule_id])
    : json({ type: 4, data: { flags: 64, content: "⚠️ 入力内容を処理できません。" } });
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
    message = { content: TEMPORARILY_UNAVAILABLE };
  }
  await editOriginal(interaction, message);
  for (const followup of message.followups || []) {
    await sendFollowup(interaction, followup);
  }
}

async function processListPageButton(interaction, env) {
  try {
    const customId = String(interaction.data?.custom_id || "");
    const selected = customId === "listselect"
      ? interaction.data?.values?.[0]
      : customId.split("|")[1];
    const message = await cmdList({ page: Number(selected) || 1 }, env);
    await editOriginal(interaction, message);
  } catch (err) {
    console.error("list page failed:", (err && err.stack) || err);
    await editOriginal(interaction, {
      content: TEMPORARILY_UNAVAILABLE,
      components: [],
    });
  }
}

async function processWatchAddButton(interaction, env) {
  let message;
  try {
    const [, icao24, registration] = String(interaction.data.custom_id || "").split("|");
    if (!HEX6.test(icao24 || "") || !registration) throw new Error("invalid watchadd custom_id");
    const typeName = (await lookupAircraftType(icao24)) || UNKNOWN_TYPE;
    const outcome = await updateWatchlist(env, (wl) => {
      if (wl[icao24] !== undefined) {
        return { changed: false, existing: normalize(wl[icao24]).label };
      }
      wl[icao24] = { label: registration.toUpperCase(), type: typeName };
      return { changed: true };
    }, `watchlist: add ${registration.toUpperCase()}`);
    message = outcome.existing
      ? { content: `ℹ️ \`${outcome.existing}\` (${icao24}) は既に登録済みです。`, components: [] }
      : { content: `✅ \`${registration.toUpperCase()}\` (${icao24} / ${typeName}) をwatchlistに追加しました。`, components: [] };
  } catch (err) {
    console.error("watchadd button failed:", (err && err.stack) || err);
    message = { content: "⚠️ 登録に失敗しました。もう一度検索してください。", components: [] };
  }
  await editOriginal(interaction, message);
}

async function processSearchPageButton(interaction, env) {
  try {
    const [, offsetText, encodedQuery, encodedAirline = ""] = String(interaction.data.custom_id || "").split("|");
    const offset = Math.max(0, Number(offsetText) || 0);
    const query = decodeURIComponent(encodedQuery || "");
    const airline = decodeURIComponent(encodedAirline || "");
    if (!query) throw new Error("missing search query");
    const r = await gh(env, "/dispatches", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        event_type: "lookup",
        client_payload: { op: "search", args: [query, airline, offset], channel_id: interaction.channel_id },
      }),
    });
    if (r.status !== 204) throw new Error(`dispatch failed: ${r.status} ${await r.text()}`);
  } catch (err) {
    console.error("search next page failed:", (err && err.stack) || err);
  }
}

async function editOriginal(interaction, message) {
  const { followups: _followups, ...visibleMessage } = message;
  const payload = { allowed_mentions: { parse: [] }, ...visibleMessage };
  if (payload.content) payload.content = payload.content.slice(0, 2000);
  const url = `${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`;
  const r = await fetch(url, {
    method: "PATCH",
    headers: { "content-type": "application/json", "user-agent": UA },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.error("editOriginal failed:", r.status, await r.text());
}

async function sendFollowup(interaction, message) {
  const privateFlags = interaction.data?.name === "airport" ? { flags: 64 } : {};
  const payload = { allowed_mentions: { parse: [] }, ...privateFlags, ...message };
  if (payload.content) payload.content = payload.content.slice(0, 2000);
  const r = await fetch(`${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}`, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": UA },
    body: JSON.stringify(payload),
  });
  if (!r.ok) console.error("sendFollowup failed:", r.status, await r.text());
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
    case "aircraft-search": return cmdAircraftSearch(o, env, interaction);
    case "remove": return cmdRemove(o, env);
    case "find": return cmdFind(o, env);
    case "list": return cmdList(o, env);
    case "info": return cmdInfo(o, env);
    case "flight": return cmdFlight(o, env, interaction);
    case "airport": return cmdAirport(o, env);
    case "menu": return cmdMenu();
    case "priority": return cmdPriority(o, env);
    case "special": return cmdSpecial(o, env);
    case "special-list": return cmdSpecialList(env);
    case "nationwide": return cmdNationwide(o, env);
    default: return { content: "未対応のコマンドです。" };
  }
}

function cmdMenu() {
  return {
    content: "✈️ **航空機通知Botメニュー**\n調べたい項目をボタンから選んでください。入力内容と結果は、基本的に実行した本人だけに表示されます。",
    components: [
      {
        type: 1,
        components: [
          { type: 2, style: 1, custom_id: "menu|aircraft-search", emoji: { name: "🔍" }, label: "機体を探して登録" },
          { type: 2, style: 2, custom_id: "menu|info", emoji: { name: "🛩️" }, label: "機体の詳細を見る" },
          { type: 2, style: 1, custom_id: "menu|flight", emoji: { name: "🛫" }, label: "飛行中の便を探す" },
          { type: 2, style: 2, custom_id: "menu|airport", emoji: { name: "🏢" }, label: "現在の発着を見る" },
        ],
      },
      {
        type: 1,
        components: [
          { type: 2, style: 2, custom_id: "menu|list", emoji: { name: "📋" }, label: "サーバー登録機を見る" },
          { type: 2, style: 2, custom_id: "menu|help", emoji: { name: "📖" }, label: "ボタンの説明" },
          { type: 2, style: 2, custom_id: "menu|refresh", emoji: { name: "🔄" }, label: "最新位置へ移動" },
          { type: 2, style: 4, custom_id: "menu|admin", emoji: { name: "⚙️" }, label: "管理者メニュー" },
        ],
      },
    ],
  };
}

function handleMenuButton(interaction, env, ctx) {
  const action = String(interaction.data.custom_id || "").split("|")[1] || "";
  if (action === "admin") {
    return isAdministrator(interaction)
      ? json({ type: 4, data: { flags: 64, ...adminPanel() } })
      : json({ type: 4, data: { flags: 64, content: "⛔ 管理画面はサーバー管理者だけが使用できます。" } });
  }
  if (action === "refresh") {
    if (!isAdministrator(interaction)) {
      return json({ type: 4, data: { flags: 64, content: "⛔ メニューの移動はサーバー管理者だけが実行できます。" } });
    }
    ctx.waitUntil(refreshMenuMessage(interaction));
    return json({ type: 6 });
  }
  if (action === "help") {
    return json({ type: 4, data: {
      flags: 64,
      content: "✈️ **主な使い方**\n`機体を探して登録`：登録記号・便名・機種から検索して登録\n`機体の詳細を見る`：登録記号またはICAO24の詳細を表示\n`飛行中の便を探す`：現在ADS-Bで確認できる便を検索\n`現在の発着を見る`：現在からの到着・出発便を表示（航空会社・機種で絞り込み可能）\n`サーバー登録機を見る`：watchlistを表示\n\n従来のスラッシュコマンドも引き続き利用できます。",
    } });
  }
  if (action === "list") {
    const synthetic = { ...interaction, data: { name: "list", options: [] } };
    ctx.waitUntil(processCommand(synthetic, env));
    return json({ type: 5, data: { flags: 64 } });
  }
  const modal = menuModal(action);
  return modal
    ? json({ type: 9, data: modal })
    : json({ type: 4, data: { flags: 64, content: "⚠️ このボタンは現在利用できません。" } });
}

function adminPanel() {
  return {
    content: "⚙️ **航空機通知Bot 管理画面**\n変更したい項目を選んでください。この画面と操作結果は管理者本人だけに表示されます。",
    components: [
      { type: 1, components: [
        { type: 2, style: 3, custom_id: "admin|add", emoji: { name: "➕" }, label: "機体を追加" },
        { type: 2, style: 4, custom_id: "admin|remove", emoji: { name: "🗑️" }, label: "機体を削除" },
        { type: 2, style: 1, custom_id: "admin|priority", emoji: { name: "⭐" }, label: "通知レベルを変更" },
      ] },
      { type: 1, components: [
        { type: 2, style: 1, custom_id: "admin|special", emoji: { name: "🚨" }, label: "期間限定SPECIALを設定" },
        { type: 2, style: 2, custom_id: "admin|nationwide", emoji: { name: "🗾" }, label: "全国通知を設定" },
      ] },
      { type: 1, components: [
        { type: 2, style: 2, custom_id: "admin|list", emoji: { name: "📋" }, label: "登録機を見る" },
        { type: 2, style: 2, custom_id: "admin|special-list", emoji: { name: "🔎" }, label: "SPECIAL機を見る" },
        { type: 2, style: 2, custom_id: "admin|status", emoji: { name: "🟢" }, label: "Botの稼働を確認" },
        { type: 2, style: 2, custom_id: "admin|help", emoji: { name: "📖" }, label: "ボタンの説明" },
      ] },
    ],
  };
}

function adminModal(action) {
  const definitions = {
    add: { title: "機体を追加", rows: [
      textInput("tail", "登録記号", "例: JA784A", true, 20),
      textInput("icao24", "ICAO24（省略可）", "例: 867F7C", false, 6),
      textInput("type", "機種（省略可）", "例: Boeing 787-8", false, 60),
    ] },
    remove: { title: "機体を削除", rows: [textInput("target", "登録記号またはICAO24", "例: JA784A", true, 20)] },
    priority: { title: "通知レベルを変更", rows: [
      textInput("aircraft", "登録記号またはICAO24", "例: JA784A", true, 20),
      textInput("level", "通知レベル", "NORMAL / WATCH / SPECIAL", true, 7),
    ] },
    special: { title: "期間SPECIALを設定", rows: [
      textInput("aircraft", "登録記号またはICAO24", "例: JA784A", true, 20),
      textInput("duration", "期間", "例: 30m / 24h / 7d", true, 10),
    ] },
    nationwide: { title: "全国通知を変更", rows: [
      textInput("aircraft", "登録記号またはICAO24", "例: JA784A", true, 20),
      textInput("enabled", "全国通知", "ON または OFF", true, 5),
    ] },
  };
  const definition = definitions[action];
  return definition ? { custom_id: `admin_modal|${action}`, title: definition.title, components: definition.rows } : null;
}

function handleAdminButton(interaction, env, ctx) {
  if (!isAdministrator(interaction)) {
    return json({ type: 4, data: { flags: 64, content: "⛔ 管理者だけが使用できます。" } });
  }
  const action = String(interaction.data.custom_id || "").split("|")[1] || "";
  if (action === "help") {
    return json({ type: 4, data: { flags: 64, content:
      "📖 **管理画面のボタン説明**\n" +
      "➕ **機体を追加**：サーバーのwatchlistへ機体を登録します。\n" +
      "🗑️ **機体を削除**：watchlistから機体を削除します。\n" +
      "⭐ **通知レベルを変更**：NORMAL・WATCH・SPECIALを設定します。\n" +
      "🚨 **期間限定SPECIALを設定**：指定期間だけSPECIALにします。\n" +
      "🗾 **全国通知を設定**：日本周辺チャンネルへの通知をON/OFFします。\n" +
      "📋 **登録機を見る**：watchlistを表示します。\n" +
      "🔎 **SPECIAL機を見る**：現在SPECIALの機体を表示します。\n" +
      "🟢 **Botの稼働を確認**：主要機能の状態を表示します。\n" +
      "📖 **ボタンの説明**：この案内を表示します。"
    } });
  }
  if (action === "status") {
    return json({ type: 4, data: { flags: 64, content: "🟢 **Bot稼働状況**\nDiscord受付: 正常\nGitHub連携: 設定済み\nADS-B検索: 有効\n\n詳細な障害が発生した場合は処理結果にエラーが表示されます。" } });
  }
  if (action === "list" || action === "special-list") {
    const synthetic = { ...interaction, data: { name: action, options: [] } };
    ctx.waitUntil(processCommand(synthetic, env));
    return json({ type: 5, data: { flags: 64 } });
  }
  const modal = adminModal(action);
  return modal
    ? json({ type: 9, data: modal })
    : json({ type: 4, data: { flags: 64, content: "⚠️ この管理操作は利用できません。" } });
}

async function refreshMenuMessage(interaction) {
  // 新しいメニューを先に作り、成功した場合だけ古いメニューを削除する。
  const created = await fetch(`${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}?wait=true`, {
    method: "POST",
    headers: { "content-type": "application/json", "user-agent": UA },
    body: JSON.stringify({ allowed_mentions: { parse: [] }, ...cmdMenu() }),
  });
  if (!created.ok) {
    console.error("refresh menu create failed:", created.status, await created.text());
    return;
  }
  const removed = await fetch(
    `${DISCORD_API}/webhooks/${interaction.application_id}/${interaction.token}/messages/@original`,
    { method: "DELETE", headers: { "user-agent": UA } },
  );
  if (!removed.ok && removed.status !== 404) {
    console.error("refresh menu delete failed:", removed.status, await removed.text());
  }
}

function textInput(customId, label, placeholder, required = true, maxLength = 100) {
  return { type: 1, components: [{
    type: 4,
    custom_id: customId,
    label,
    style: 1,
    placeholder,
    required,
    max_length: maxLength,
  }] };
}

function personalModal(action) {
  const definitions = {
    aircraft_add: { title: "個人機体通知を追加", rows: [
      textInput("aircraft", "登録記号またはICAO24", "例: JA784A", true, 20),
      textInput("level", "通知レベル", "NORMAL または SPECIAL", true, 7),
    ] },
    aircraft_remove: { title: "個人機体通知を解除", rows: [
      textInput("aircraft", "登録記号またはICAO24", "例: JA784A", true, 20),
    ] },
    destination_add: { title: "個人早期通知を追加", rows: [
      textInput("aircraft", "登録記号またはICAO24", "例: A7-BBA", true, 20),
      textInput("destination", "目的空港", "例: NRT / RJAA", true, 4),
    ] },
    destination_remove: { title: "個人早期通知を解除", rows: [
      textInput("rule_id", "登録ID", "例: 1", true, 8),
    ] },
    airport_add: { title: "空港ウォッチを追加", rows: [
      textInput("airport", "空港コード", "例: HND / NRT / CTS", true, 3),
      textInput("radius", "通知半径（km）", "例: 50", false, 3),
    ] },
    airport_remove: { title: "空港ウォッチを解除", rows: [
      textInput("airport", "空港コード", "例: HND / NRT / CTS", true, 3),
    ] },
    equipment_add: { title: "個人機材通知を追加", rows: [
      textInput("flight", "便名", "例: JL12", true, 12),
      textInput("equipment", "機材コード", "例: B77W", true, 12),
    ] },
    equipment_remove: { title: "個人機材通知を解除", rows: [
      textInput("rule_id", "登録ID", "例: 1", true, 8),
    ] },
  };
  const definition = definitions[action];
  return definition ? { custom_id: `personal_modal|${action}`, title: definition.title, components: definition.rows } : null;
}

function personalUserId(interaction) {
  return String(interaction.member?.user?.id || interaction.user?.id || "");
}

function cleanBridgeValue(value) {
  return String(value || "").replace(/[|\r\n]/g, " ").trim();
}

function personalBridge(interaction, operation, values = []) {
  const userId = personalUserId(interaction);
  if (!/^\d+$/.test(userId)) {
    return json({ type: 4, data: { flags: 64, content: "⚠️ 利用者を確認できませんでした。" } });
  }
  const payload = ["__PERSONAL_PANEL__", userId, operation, ...values.map(cleanBridgeValue)].join("|");
  return json({ type: 4, data: { content: payload, allowed_mentions: { parse: [] } } });
}

function personalAlertBridge(interaction, operation, values = []) {
  const userId = personalUserId(interaction);
  const actionId = String(interaction.id || "");
  if (!/^\d+$/.test(userId) || !/^\d+$/.test(actionId)) {
    return json({ type: 4, data: { flags: 64, content: "⚠️ 操作情報を確認できませんでした。" } });
  }
  // DiscordのBot返信そのものを無料の永続キューとして使う。Macが停止中でも
  // このメッセージが残り、Bot再接続時に処理される。
  const payload = ["__PERSONAL_ALERT__", userId, operation, actionId, ...values.map(cleanBridgeValue)].join("|");
  return json({ type: 4, data: {
    content: `⏳ 通知設定の変更を受け付けました。\n||${payload}||`,
    allowed_mentions: { parse: [] },
  } });
}

function personalAlertError(message) {
  return json({ type: 4, data: { flags: 64, content: message } });
}

function personalAlertIcao24(interaction) {
  for (const embed of interaction.message?.embeds || []) {
    for (const field of embed.fields || []) {
      if (String(field.name || "").toUpperCase() !== "ICAO24") continue;
      const match = String(field.value || "").match(/[0-9a-fA-F]{6}/);
      if (match) return match[0].toLowerCase();
    }
  }
  return "";
}

function clickedComponent(interaction) {
  const customId = String(interaction.data?.custom_id || "");
  for (const row of interaction.message?.components || []) {
    for (const component of row.components || []) {
      if (String(component.custom_id || "") === customId) return component;
    }
  }
  return null;
}

function handlePersonalAlertButton(interaction) {
  const parts = String(interaction.data?.custom_id || "").split("|");
  const action = parts[1] || "";
  const ownerId = parts[2] || "";
  const userId = personalUserId(interaction);
  if (!/^\d+$/.test(ownerId) || ownerId !== userId) {
    return personalAlertError("この通知は登録者本人だけが操作できます。");
  }
  if (action === "mute") {
    const duration = ["1h", "6h", "morning", "24h"].includes(parts[3]) ? parts[3] : "24h";
    return personalAlertBridge(interaction, "alert_mute", [duration]);
  }
  if (action === "stop") {
    const icao24 = HEX6.test(parts[3] || "") ? parts[3].toLowerCase() : personalAlertIcao24(interaction);
    const airport = String(parts[4] || "").toUpperCase();
    if (!icao24 && !airport) return personalAlertError("⚠️ 対象の機体を確認できませんでした。");
    return personalAlertBridge(interaction, "alert_stop", [icao24, airport]);
  }
  return personalAlertError("⚠️ この通知操作には対応していません。");
}

function handleLegacyPersonalAlertButton(interaction) {
  // 旧通知のランダムcustom_idはラベルを見ないと操作を判別できない。
  // サーバー内の一般ボタンを誤認しないよう、DMかつ既知ラベルだけに限定する。
  if (interaction.guild_id) return null;
  const component = clickedComponent(interaction);
  const label = String(component?.label || "");
  if (label === "個人通知を24時間休止") {
    return personalAlertBridge(interaction, "alert_mute", ["24h"]);
  }
  if (label === "この機体の通知を停止" || /^(?:[A-Z0-9]{3,4})の空港通知を停止$/.test(label)) {
    const icao24 = personalAlertIcao24(interaction);
    const airport = label.match(/^([A-Z0-9]{3,4})の空港通知を停止$/)?.[1] || "";
    if (!icao24 && !airport) return personalAlertError("⚠️ 対象の機体を確認できませんでした。");
    return personalAlertBridge(interaction, "alert_stop", [icao24, airport]);
  }
  return null;
}

function handlePersonalPanelButton(interaction) {
  const action = String(interaction.data.custom_id || "").split(":")[1] || "";
  if (action === "launcher_help") {
    return json({ type: 4, data: { flags: 64, content:
      "📖 **個人設定を作るパネル**\n" +
      "🔒 **自分専用の通知設定を作る**：本人とBotだけが見られる個人設定チャンネルを作ります。\n" +
      "🔄 **最新位置へ移動**：管理者がこのパネルをチャンネルの一番下へ移動します。\n" +
      "📖 **ボタンの説明**：この案内を表示します。"
    } });
  }
  if (action === "help") {
    return json({ type: 4, data: { flags: 64, content:
      "📖 **個人設定のボタン説明**\n" +
      "各通知の **追加** は新しく登録、**見る** は登録内容の確認、**解除** は登録の削除に使います。\n" +
      "✈️ **機体通知**：指定した機体を日本国内で検出したときに通知します。\n" +
      "🌍 **目的地早期通知**：指定機体が指定空港へ向かうと判明したときに通知します。\n" +
      "🏢 **空港ウォッチ**：指定空港の周辺で対象機を検出したときに通知します。\n" +
      "🔔 **機材通知**：指定便に指定機材が投入されたときに通知します。\n" +
      "⚙️ **通知ON・条件設定**：個人通知のON/OFFや条件を変更します。\n" +
      "📋 **全登録・設定を見る**：登録数と通知状態を表示します。\n" +
      "🔄 **パネルを一番下へ**：パネルをチャンネルの一番下へ移動します。\n" +
      "🗑️ **個人設定を全削除**：チャンネルと個人通知データをすべて削除します。\n" +
      "📖 **この画面の使い方**：この案内を表示します。"
    } });
  }
  if (action === "create") {
    return personalBridge(interaction, "create");
  }
  if (action === "launcher_refresh") {
    return isAdministrator(interaction)
      ? personalBridge(interaction, "launcher_refresh")
      : json({ type: 4, data: { flags: 64, content: "⛔ パネルの移動はサーバー管理者だけが実行できます。" } });
  }
  if (action === "summary") return personalBridge(interaction, "summary");
  if (action === "refresh") return personalBridge(interaction, "refresh");
  if (action === "delete") {
    return json({ type: 4, data: { flags: 64, content: "⚠️ **個人チャンネルを削除しますか？**\n個人機体通知・目的地早期通知・空港ウォッチ・機材投入通知・通知条件もすべて削除され、個人通知は停止します。この操作は元に戻せません。", components: [
      { type: 1, components: [
        { type: 2, style: 4, custom_id: "personal_action|channel_delete_confirm", emoji: { name: "🗑️" }, label: "チャンネルと個人データを削除" },
      ] },
    ] } });
  }
  if (["aircraft", "airport", "equipment"].includes(action)) {
    const labels = { aircraft: "✈️ **機体通知**", airport: "🏢 **空港ウォッチ**", equipment: "🔔 **機材投入通知**" };
    const buttonLabels = {
      aircraft: ["機体を通知登録", "機体の登録一覧", "機体の登録解除"],
      airport: ["空港周辺を通知登録", "空港の登録一覧", "空港の登録解除"],
      equipment: ["便・機材を通知登録", "便・機材の登録一覧", "便・機材の登録解除"],
    }[action];
    return json({ type: 4, data: { flags: 64, content: labels[action], components: [
      { type: 1, components: [
        { type: 2, style: 3, custom_id: `personal_action|${action}_add`, emoji: { name: "➕" }, label: buttonLabels[0] },
        { type: 2, style: 1, custom_id: `personal_action|${action}_list`, emoji: { name: "📋" }, label: buttonLabels[1] },
        { type: 2, style: 4, custom_id: `personal_action|${action}_remove`, emoji: { name: "🗑️" }, label: buttonLabels[2] },
      ] },
    ] } });
  }
  if (action === "destination") {
    return json({ type: 4, data: { flags: 64, content: "🌍 **目的地早期通知**", components: [
      { type: 1, components: [
        { type: 2, style: 3, custom_id: "personal_action|destination_add", emoji: { name: "🌍" }, label: "目的地を通知登録" },
        { type: 2, style: 1, custom_id: "personal_action|destination_list", emoji: { name: "📋" }, label: "目的地の登録一覧" },
        { type: 2, style: 4, custom_id: "personal_action|destination_remove", emoji: { name: "🗑️" }, label: "目的地の登録解除" },
      ] },
    ] } });
  }
  if (action === "settings") {
    return json({ type: 4, data: { flags: 64, content: "⚙️ **個人通知設定**", components: [
      { type: 1, components: [
        { type: 2, style: 1, custom_id: "personal_action|summary", emoji: { name: "📋" }, label: "設定を表示" },
        { type: 2, style: 2, custom_id: "personal_action|settings_toggle", emoji: { name: "🔔" }, label: "全通知をON/OFF" },
        { type: 2, style: 2, custom_id: "personal_action|filters_reset", emoji: { name: "↩️" }, label: "通知条件をリセット" },
      ] },
    ] } });
  }
  return json({ type: 4, data: { flags: 64, content: "⚠️ この個人設定は利用できません。" } });
}

function handlePersonalPanelAction(interaction) {
  const action = String(interaction.data.custom_id || "").split("|")[1] || "";
  if (["aircraft_add", "aircraft_remove", "destination_add", "destination_remove", "airport_add", "airport_remove", "equipment_add", "equipment_remove"].includes(action)) {
    return json({ type: 9, data: personalModal(action) });
  }
  if (["aircraft_list", "destination_list", "airport_list", "equipment_list", "summary", "settings_toggle", "filters_reset"].includes(action)) {
    return personalBridge(interaction, action);
  }
  if (action === "channel_delete_confirm") return personalBridge(interaction, "channel_delete");
  return json({ type: 4, data: { flags: 64, content: "⚠️ この操作は利用できません。" } });
}

function personalModalSubmit(interaction) {
  const action = String(interaction.data.custom_id || "").split("|")[1] || "";
  const values = {};
  for (const row of interaction.data?.components || []) {
    for (const field of row.components || []) values[field.custom_id] = cleanBridgeValue(field.value);
  }
  const args = {
    aircraft_add: [values.aircraft, values.level || "NORMAL"],
    aircraft_remove: [values.aircraft],
    destination_add: [values.aircraft, values.destination],
    destination_remove: [values.rule_id],
    airport_add: [values.airport, values.radius || "50"],
    airport_remove: [values.airport],
    equipment_add: [values.flight, values.equipment],
    equipment_remove: [values.rule_id],
  }[action];
  return args ? personalBridge(interaction, action, args)
    : json({ type: 4, data: { flags: 64, content: "⚠️ 入力内容を処理できません。" } });
}

function menuModal(action) {
  const definitions = {
    "aircraft-search": {
      title: "機体を検索",
      rows: [
        textInput("query", "検索する機体", "例: JA78 / 8691AA / JL12 / B77W", true, 50),
        textInput("airline", "航空会社（省略可）", "例: ANA / NH / JAL / JL", false, 20),
      ],
    },
    info: {
      title: "機体情報を表示",
      rows: [textInput("aircraft", "登録記号またはICAO24", "例: JA784A / 867F7C", true, 20)],
    },
    flight: {
      title: "運航中の便を検索",
      rows: [textInput("query", "便名・登録記号・ICAO24", "例: JL12 / JAL12 / JA784A", true, 50)],
    },
    airport: {
      title: "空港の発着予定",
      rows: [
        textInput("airport", "空港コード", "例: HND / RJTT", true, 4),
        textInput("type", "表示する便（省略可）", "both / arrival / departure", false, 9),
        textInput("hours", "時間範囲（省略可・1〜12時間）", "例: 3", false, 2),
        textInput("airline", "航空会社（省略可）", "例: ANA / NH / JAL", false, 40),
        textInput("aircraft", "機種（省略可）", "例: B789 / 787 / A350", false, 40),
      ],
    },
    "airport-date": {
      title: "日付を指定して発着予定を表示",
      rows: [
        textInput("airport", "空港コード", "例: HND / RJTT", true, 4),
        textInput("date", "日付（空港の現地日付）", "例: 2026-10-03", true, 10),
        textInput("start_time", "開始時刻（省略時 00:00）", "例: 15:00", false, 5),
        textInput("hours", "時間範囲（省略時12・最大12時間）", "例: 6", false, 2),
        textInput("type", "表示する便（省略可）", "both / arrival / departure", false, 9),
      ],
    },
    "airport-airline": {
      title: "航空会社で発着予定を絞り込み",
      rows: [
        textInput("airport", "空港コード", "例: HND / RJTT", true, 4),
        textInput("airline", "航空会社名・コード", "例: ANA / NH / JAL / JL", true, 40),
        textInput("date", "日付（省略時は現在から）", "例: 2026-10-03", false, 10),
        textInput("type", "表示する便（省略可）", "both / arrival / departure", false, 9),
        textInput("hours", "時間範囲（1〜12時間）", "例: 3", false, 2),
      ],
    },
  };
  const definition = definitions[action];
  if (!definition) return null;
  return { custom_id: `menu_modal|${action}`, title: definition.title, components: definition.rows };
}

function menuModalToCommand(interaction, command) {
  const values = {};
  for (const row of interaction.data?.components || []) {
    for (const field of row.components || []) values[field.custom_id] = String(field.value || "").trim();
  }
  const options = Object.entries(values)
    .filter(([, value]) => value !== "")
    .map(([name, value]) => {
      if (name === "hours") return { name, value: Number(value) };
      if (name === "enabled") return { name, value: ["on", "true", "1", "yes"].includes(value.toLowerCase()) };
      return { name, value };
    });
  return { ...interaction, data: { name: ["airport-date", "airport-airline"].includes(command) ? "airport" : command, options } };
}

async function cmdAircraftSearch(o, env, interaction) {
  const query = String(o.query || "").trim();
  const airline = String(o.airline || "").trim();
  if (query.replace(/[\s-]/g, "").length < 3) {
    return { content: "⚠️ 3文字以上入力してください。例: `/aircraft-search query:JA78`" };
  }
  return startSlowLookup(env, "search", [query, airline, 0], interaction, {
    failure: `❓ \`${query}\` に一致する機体を見つけられませんでした。`,
  });
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
  const now = Date.now() / 1000;
  const items = Object.entries(data).filter(([, value]) => {
    if (String(value?.priority || "NORMAL").toUpperCase() !== "SPECIAL") return false;
    const until = Number(value?.special_until || 0);
    return !until || until > now;
  });
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
  let data = {};
  try {
    ({ data } = await readWatchlist(env));
  } catch (err) {
    console.error("primary watchlist read failed in /info:", err?.message || err);
  }
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
  // Cloudflare側のリポジトリ変数やブランチ設定が一時的にずれていても、
  // 公開中の正規watchlistを参照して登録機を見失わないようにする。
  if (!entry && !icao24) {
    const canonical = await readCanonicalWatchlist();
    const found = Object.entries(canonical).find(([id, value]) =>
      id.toUpperCase() === compact || normalize(value).label.toUpperCase() === query,
    );
    if (found) {
      [icao24, entry] = found;
    }
  }
  if (!icao24) icao24 = await lookupIcao24(query);
  if (!icao24) return { content: `❓ \`${query}\` の機体情報は見つかりませんでした。` };

  const normalized = entry ? normalize(entry) : { label: query, type: UNKNOWN_TYPE };
  const [details, liveAircraft] = await Promise.all([
    lookupAircraftDetails(icao24),
    adsbLookup("hex", icao24),
  ]);
  const type = normalized.type !== UNKNOWN_TYPE ? normalized.type : details.type || UNKNOWN_TYPE;
  const priority = effectivePriority(entry);
  const year = details.year || "不明";
  const age = aircraftAge(details.year);
  const yearNote = details.yearSource === "first-flight" ? "（初飛行年を基準）" : "";
  const live = liveAircraft[0] || null;
  const status = live ? (live.alt_baro === "ground" ? "🟢 地上で受信中" : "🟢 飛行中") : "⚪ 現在位置なし";
  const specialUntil = priority === "SPECIAL" && Number(entry?.special_until || 0) > Date.now() / 1000
    ? `（<t:${Math.floor(Number(entry.special_until))}:R>まで）`
    : "";
  const liveLines = live
    ? `\nコールサイン: \`${String(live.flight || "不明").trim() || "不明"}\n` +
      `現在状態: ${status}` +
      (num(live.alt_baro) ? `\n高度: ${fmt0(live.alt_baro)} ft` : "") +
      (num(live.gs) ? `\n速度: ${fmt0(live.gs * 1.852)} km/h` : "")
    : `\n現在状態: ${status}`;
  return {
    content: `✈️ **機体情報**\n` +
      `登録記号: \`${normalized.label}\`\n` +
      `icao24: \`${icao24}\`\n` +
      `機種: ${type}\n` +
      `製造年: ${year}${yearNote}${age ? `（機齢 約${age}年）` : ""}\n` +
      `運航会社: ${details.operator || "不明"}\n` +
      `登録国: ${details.country || "不明"}\n` +
      `通知レベル: **${priority}**${specialUntil}\n` +
      `全国通知: **${entry?.nationwide_alert === true ? "ON" : "OFF"}**` +
      (entry ? "\nwatchlist: 👁️ 登録済み" : "\nwatchlist: 未登録") +
      liveLines +
      `\n地図: https://globe.adsbexchange.com/?icao=${icao24}`,
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
  const pageOptions = Array.from({ length: Math.min(pages, 25) }, (_, index) => ({
    label: `${index + 1}ページ`,
    value: String(index + 1),
    description: `${(index * PAGE_SIZE) + 1}〜${Math.min((index + 1) * PAGE_SIZE, items.length)}機目`,
    default: index + 1 === page,
  }));
  return {
    content: `📋 watchlist 全${items.length}機(ページ ${page}/${pages})\n${lines.join("\n")}`.slice(0, 1990),
    components: [
      {
        type: 1,
        components: [
          { type: 2, style: 2, custom_id: `listpage|${Math.max(1, page - 1)}`, label: "◀ 前へ", disabled: page <= 1 },
          { type: 2, style: 2, custom_id: `listpage|${Math.min(pages, page + 1)}`, label: "次へ ▶", disabled: page >= pages },
        ],
      },
      {
        type: 1,
        components: [{
          type: 3,
          custom_id: "listselect",
          placeholder: "表示するページを選択",
          min_values: 1,
          max_values: 1,
          options: pageOptions,
        }],
      },
    ],
  };
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

async function cmdAirport(o, env) {
  const code = String(o.airport || "").trim().toUpperCase();
  const direction = String(o.type || "both").toLowerCase();
  const date = String(o.date || "").trim();
  const startTime = String(o.start_time || "").trim() || "00:00";
  const hours = Math.max(1, Math.min(Number(o.hours) || (date ? 12 : 3), 12));
  const airline = String(o.airline || "").trim();
  const aircraft = String(o.aircraft || "").trim();
  const showAll = o.show_all === true;
  if (!/^[A-Z]{3,4}$/.test(code)) {
    return { content: "⚠️ 空港は3文字のIATA（例: HND）または4文字のICAO（例: RJTT）で指定してください。" };
  }
  if (date && !isValidLocalDate(date)) {
    return { content: "⚠️ 日付は `YYYY-MM-DD` 形式の実在する日付で指定してください。例: `2026-10-03`" };
  }
  if (o.start_time && !date) {
    return { content: "⚠️ `start_time`を使用する場合は`date`も指定してください。" };
  }
  if (date && !/^([01]\d|2[0-3]):[0-5]\d$/.test(startTime)) {
    return { content: "⚠️ 開始時刻は24時間表記の `HH:MM` で指定してください。例: `15:00`" };
  }
  if (!env.AERODATABOX_RAPIDAPI_KEY) {
    return {
      content: "⚠️ 空港の発着予定は現在一時的に利用できません。管理者側のAPI設定を確認してください。",
    };
  }

  const codeType = code.length === 3 ? "iata" : "icao";
  const query = new URLSearchParams({
    direction: direction === "arrival" ? "Arrival" : direction === "departure" ? "Departure" : "Both",
    withLeg: "true",
    withCancelled: "true",
    withCodeshared: "false",
    withCargo: "true",
    withPrivate: "false",
    withLocation: "false",
  });
  let endpoint = `https://aerodatabox.p.rapidapi.com/flights/airports/${codeType}/${code}`;
  if (date) {
    const fromLocal = `${date}T${startTime}`;
    const [hour, minute] = startTime.split(":").map(Number);
    const end = new Date(Date.UTC(2000, 0, 1, hour, minute + hours * 60));
    const endDate = new Date(`${date}T00:00:00Z`);
    endDate.setUTCDate(endDate.getUTCDate() + Math.floor((hour * 60 + minute + hours * 60) / 1440));
    const toLocal = `${endDate.toISOString().slice(0, 10)}T${String(end.getUTCHours()).padStart(2, "0")}:${String(end.getUTCMinutes()).padStart(2, "0")}`;
    endpoint += `/${encodeURIComponent(fromLocal)}/${encodeURIComponent(toLocal)}`;
  } else {
    query.set("offsetMinutes", "0");
    query.set("durationMinutes", String(hours * 60));
  }
  let response;
  try {
    response = await fetch(`${endpoint}?${query}`, {
      headers: {
        "x-rapidapi-key": env.AERODATABOX_RAPIDAPI_KEY,
        "x-rapidapi-host": "aerodatabox.p.rapidapi.com",
        "user-agent": UA,
      },
      signal: AbortSignal.timeout(8000),
    });
  } catch (err) {
    console.error("airport schedule failed:", err?.name, err?.message);
    return { content: "⚠️ 空港の発着予定は現在一時的に利用できません。少し待ってから再度お試しください。" };
  }
  if (response.status === 204 || response.status === 404) {
    return { content: `❓ 空港 \`${code}\` の発着予定は見つかりませんでした。コードを確認してください。` };
  }
  if (!response.ok) {
    console.error("airport schedule HTTP error:", response.status, await response.text());
    const hint = [401, 403, 429].includes(response.status) ? "APIキー・契約枠・利用上限を確認してください。" : "少し待ってから再試行してください。";
    return { content: `⚠️ 空港の発着予定は現在一時的に利用できません。${hint}` };
  }
  const schedule = await response.json();
  const { data: watchlist } = await readWatchlist(env);
  return buildAirportMessage(code, hours, direction, schedule, watchlist, {
    airline,
    aircraft,
    showAll,
    date,
    startTime,
  });
}

function isValidLocalDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.getTime()) && parsed.toISOString().slice(0, 10) === value;
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
  let r;
  try {
    r = await gh(env, "/dispatches", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ event_type: "lookup", client_payload: { op, args, channel_id: interaction.channel_id } }),
    });
  } catch (err) {
    console.error("dispatch failed:", err?.name, err?.message);
    return { content: TEMPORARILY_UNAVAILABLE };
  }
  if (r.status !== 204) {
    console.error("dispatch failed:", r.status, await r.text());
    return { content: TEMPORARILY_UNAVAILABLE };
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
  const repo = String(env.GITHUB_REPO || DEFAULT_GITHUB_REPO).trim();
  return fetch(`https://api.github.com/repos/${repo}${path}`, {
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
  if (r.status === 404) {
    throw new Error("GitHub上のwatchlist.jsonまたは参照先リポジトリが見つかりません");
  }
  if (!r.ok) throw new Error(`GitHub read failed: ${r.status} ${await r.text()}`);
  const file = await r.json();
  const parsed = JSON.parse(b64ToUtf8(file.content) || "{}");
  return { data: parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {}, sha: file.sha };
}

async function readCanonicalWatchlist() {
  const url = `https://raw.githubusercontent.com/${DEFAULT_GITHUB_REPO}/main/watchlist.json`;
  const data = await fetchJson(url, 5000);
  return data && typeof data === "object" && !Array.isArray(data) ? data : {};
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
    return { ...value, label: value.label || "?", type: value.type || UNKNOWN_TYPE };
  }
  return { label: String(value), type: UNKNOWN_TYPE };
}

function effectivePriority(value, now = Date.now() / 1000) {
  if (!value || typeof value !== "object") return "NORMAL";
  const priority = String(value.priority || "NORMAL").toUpperCase();
  if (priority !== "SPECIAL" || !value.special_until) return priority;
  if (Number(value.special_until) > now) return "SPECIAL";
  return String(value.priority_after_special || "NORMAL").toUpperCase();
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
      signal: AbortSignal.timeout(4000),
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
  const directYear = firstYear(
    data.Year,
    data.YearOfManufacture,
    data.ManufactureYear,
    data.Manufactured,
    data.Built,
    data.BuildYear,
    data.FirstFlight,
    data.FirstFlightDate,
    data.FirstRegistered,
    data.Registered,
    adsb.year,
    adsb.year_built,
    adsb.built,
    adsb.first_flight_date,
  );
  const registration = data.Registration || adsb.registration || null;
  const wikidataYear = directYear ? null : await lookupWikidataAircraftYear(registration);
  return {
    type: (
      `${data.Manufacturer || ""} ${data.Type || data.ICAOTypeCode || ""}`.trim()
      || `${adsb.manufacturer || ""} ${adsb.type || adsb.icao_type || ""}`.trim()
      || null
    ),
    year: directYear || wikidataYear,
    yearSource: directYear ? "database" : wikidataYear ? "first-flight" : null,
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

function firstYear(...values) {
  const currentYear = new Date().getUTCFullYear();
  for (const value of values) {
    const match = String(value || "").match(/(?:19|20)\d{2}/);
    const year = match ? Number(match[0]) : 0;
    if (year >= 1903 && year <= currentYear) return String(year);
  }
  return null;
}

async function lookupWikidataAircraftYear(registration) {
  if (!registration) return null;
  const search = await fetchJson(
    "https://www.wikidata.org/w/api.php?action=wbsearchentities&format=json&language=en&limit=3&search="
      + encodeURIComponent(registration),
    3500,
  );
  const exact = (search?.search || []).find((item) =>
    String(item.label || "").toUpperCase() === String(registration).toUpperCase()
    || String(item.description || "").toUpperCase().includes(String(registration).toUpperCase()),
  );
  if (!exact?.id) return null;
  const entityData = await fetchJson(
    `https://www.wikidata.org/wiki/Special:EntityData/${exact.id}.json`,
    3500,
  );
  const claims = entityData?.entities?.[exact.id]?.claims || {};
  // P606=初飛行、P571=開始日。製造年が無い場合の近似値として使う。
  const claimTime = (claims.P606 || claims.P571 || [])
    .map((claim) => claim?.mainsnak?.datavalue?.value?.time)
    .find(Boolean);
  return firstYear(claimTime);
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

function watchlistBadge(watchlist, movement) {
  const aircraft = movement?.aircraft || {};
  const icao24 = String(aircraft.modeS || aircraft.icao24 || "").toLowerCase();
  const registration = String(aircraft.reg || aircraft.registration || "").replace(/[\s-]/g, "").toUpperCase();
  let entry = icao24 ? watchlist[icao24] : null;
  if (!entry && registration) {
    entry = Object.values(watchlist).find((value) =>
      normalize(value).label.replace(/[\s-]/g, "").toUpperCase() === registration,
    );
  }
  if (!entry) return "";
  const priority = effectivePriority(entry);
  return priority === "SPECIAL" ? " 🚨SPECIAL" : priority === "WATCH" ? " 👁️WATCH" : " 👁️監視中";
}

function movementTime(movement) {
  return movement?.revisedTime?.local
    || movement?.predictedTime?.local
    || movement?.scheduledTime?.local
    || movement?.runwayTime?.local
    || "";
}

function airportAircraft(item, movement) {
  return movement?.aircraft || item.aircraft || {};
}

function airportAirlineText(item) {
  const airline = item.airline || {};
  return [airline.name, airline.iata, airline.icao, item.number, item.callSign]
    .filter(Boolean)
    .join(" ")
    .toUpperCase();
}

function airportAircraftText(item, movement) {
  const aircraft = airportAircraft(item, movement);
  return [aircraft.model, aircraft.reg, aircraft.registration, aircraft.modeS, aircraft.icao24]
    .filter(Boolean)
    .join(" ")
    .toUpperCase();
}

function airportFlightLine(item, kind, watchlist) {
  const movement = kind === "arrival" ? item.arrival : item.departure;
  const opposite = kind === "arrival" ? item.departure?.airport : item.arrival?.airport;
  const time = movementTime(movement);
  const hhmm = time.match(/T(\d{2}:\d{2})/)?.[1] || "--:--";
  const flight = item.number || item.callSign || "便名不明";
  const airport = opposite?.iata || opposite?.icao || "---";
  const aircraft = airportAircraft(item, movement);
  const type = aircraft.model || aircraft.modeS || "";
  const status = String(item.status || "").toLowerCase();
  const icon = status.includes("cancel") ? "🔴" : status.includes("arriv") || status.includes("land") ? "🔵" : status.includes("depart") || status.includes("airborne") ? "🟢" : "🟡";
  const gate = movement?.gate ? ` G${movement.gate}` : "";
  const badgeMovement = { ...movement, aircraft: Object.keys(aircraft).length ? aircraft : movement?.aircraft };
  return `${icon} \`${hhmm}\` **${flight}** ${kind === "arrival" ? "←" : "→"} ${airport}${type ? ` · ${type}` : ""}${gate}${watchlistBadge(watchlist, badgeMovement)}`;
}

function splitAirportMessages(header, sections, footer) {
  const messages = [];
  let current = "";
  const lines = [header, "", ...sections.flatMap((section, index) => [section, ...(index < sections.length - 1 ? [""] : [])]), "", footer];
  for (const line of lines.flatMap((value) => String(value).split("\n"))) {
    const addition = `${current ? "\n" : ""}${line}`;
    if ((current + addition).length > 1900 && current) {
      messages.push(current);
      current = line;
    } else {
      current += addition;
    }
  }
  if (current) messages.push(current);
  return messages;
}

function buildAirportMessage(code, hours, direction, schedule, watchlist, filters = {}) {
  const groups = [];
  if (direction !== "departure") groups.push(["到着", "arrival", schedule.arrivals || []]);
  if (direction !== "arrival") groups.push(["出発", "departure", schedule.departures || []]);
  const sections = [];
  let matchedCount = 0;
  const airlineFilter = String(filters.airline || "").toUpperCase();
  const aircraftFilter = String(filters.aircraft || "").toUpperCase();
  for (const [label, kind, flights] of groups) {
    const filtered = flights.filter((item) => {
      const movement = kind === "arrival" ? item.arrival : item.departure;
      return (!airlineFilter || airportAirlineText(item).includes(airlineFilter))
        && (!aircraftFilter || airportAircraftText(item, movement).includes(aircraftFilter));
    });
    const sorted = [...filtered].sort((a, b) => movementTime(kind === "arrival" ? a.arrival : a.departure).localeCompare(movementTime(kind === "arrival" ? b.arrival : b.departure)));
    matchedCount += sorted.length;
    const visible = filters.showAll ? sorted : sorted.slice(0, 12);
    const lines = visible.map((item) => airportFlightLine(item, kind, watchlist));
    const omitted = !filters.showAll && sorted.length > visible.length ? `\n…ほか ${sorted.length - visible.length}便（show_all:true で全便表示）` : "";
    sections.push(`**${kind === "arrival" ? "🛬" : "🛫"} ${label}（${sorted.length}便）**\n${lines.length ? lines.join("\n") : "該当便なし"}${omitted}`);
  }
  const airport = schedule.airport || {};
  const title = airport.name ? `${airport.name}（${airport.iata || code} / ${airport.icao || code}）` : code;
  const legend = "🟢運航中 · 🟡予定 · 🔵到着済み · 🔴欠航";
  const filterLabels = [
    filters.airline ? `航空会社: ${filters.airline}` : "",
    filters.aircraft ? `機種: ${filters.aircraft}` : "",
  ].filter(Boolean);
  const filterLine = filterLabels.length ? `\n絞り込み: ${filterLabels.join(" / ")}` : "";
  const rangeLabel = filters.date
    ? `${filters.date} ${filters.startTime || "00:00"}から${hours}時間（空港現地時刻）`
    : `今から${hours}時間`;
  const header = `🏢 **${title} 発着予定**（${rangeLabel}・${matchedCount}便）${filterLine}\n${legend}`;
  const messages = splitAirportMessages(header, sections, "Data: AeroDataBox");
  return {
    content: messages[0] || `${header}\n\n該当便なし`,
    followups: messages.slice(1).map((content) => ({ content })),
  };
}
