import test from "node:test";
import assert from "node:assert/strict";
import worker, { MAIN_KEYBOARD, NEW_VIDEO_BUTTON, isNewVideo } from "./telegram-worker.js";

const env = {
  TELEGRAM_BOT_TOKEN: "tok",
  TELEGRAM_WEBHOOK_SECRET: "whsec",
  TELEGRAM_CHAT_ID: "111",
  TELEGRAM_USER_ID: "222",
  GITHUB_OWNER: "owner",
  GITHUB_REPO: "repo",
  GITHUB_TOKEN: "ghp",
};

function installFetch({ githubStatus = 204, telegramOk = true } = {}) {
  const calls = [];
  globalThis.fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url: String(url), body });
    if (String(url).startsWith("https://api.github.com/")) {
      return new Response(null, { status: githubStatus });
    }
    if (!telegramOk) throw new Error("telegram down");
    return new Response(JSON.stringify({ ok: true, result: {} }), { status: 200 });
  };
  return calls;
}

const github = (calls) => calls.filter((c) => c.url.startsWith("https://api.github.com/"));
const tg = (calls, method) => calls.filter((c) => c.url.endsWith(`/${method}`));

function post(update, { secret = "whsec" } = {}) {
  return worker.fetch(
    new Request("https://worker.example/", {
      method: "POST",
      headers: { "x-telegram-bot-api-secret-token": secret, "content-type": "application/json" },
      body: JSON.stringify(update),
    }),
    env,
  );
}

const textUpdate = (text, { chat = 111, user = 222 } = {}) => ({
  update_id: 1,
  message: { chat: { id: chat }, from: { id: user }, text },
});

const callbackUpdate = (data) => ({
  update_id: 2,
  callback_query: { id: "cb1", data, from: { id: 222 }, message: { chat: { id: 111 } } },
});

test("persistent keyboard has the exact required fields", () => {
  assert.deepEqual(MAIN_KEYBOARD, {
    keyboard: [[{ text: "🎬 فيديو جديد" }]],
    resize_keyboard: true,
    is_persistent: true,
  });
  assert.equal(MAIN_KEYBOARD.resize_keyboard, true);
  assert.equal(MAIN_KEYBOARD.is_persistent, true);
  assert.ok(!("one_time_keyboard" in MAIN_KEYBOARD));
  assert.equal(NEW_VIDEO_BUTTON, "🎬 فيديو جديد");
});

test("/start replies with the keyboard and does not dispatch", async () => {
  const calls = installFetch();
  const response = await post(textUpdate("/start"));
  assert.equal(response.status, 200);
  assert.equal(github(calls).length, 0);
  const sent = tg(calls, "sendMessage");
  assert.equal(sent.length, 1);
  assert.deepEqual(sent[0].body.reply_markup, MAIN_KEYBOARD);
  assert.ok(!("one_time_keyboard" in sent[0].body.reply_markup));
});

for (const text of ["🎬 فيديو جديد", "فيديو جديد", "/new", "  فيديو   جديد ", "/video", "/episode", "حلقة جديدة", "أنشئ فيديو جديد", "انشئ فيديو جديد"]) {
  test(`new-video trigger "${text}" dispatches production once`, async () => {
    const calls = installFetch();
    const response = await post(textUpdate(text));
    assert.equal(response.status, 200);
    const dispatches = github(calls);
    assert.equal(dispatches.length, 1);
    assert.ok(dispatches[0].url.endsWith("/workflows/production.yml/dispatches"));
    assert.deepEqual(dispatches[0].body, { ref: "main", inputs: { trigger_source: "telegram" } });
    const sent = tg(calls, "sendMessage");
    assert.equal(sent.length, 1);
    assert.deepEqual(sent[0].body.reply_markup, MAIN_KEYBOARD);
  });
}

test("isNewVideo rejects unrelated text", () => {
  assert.equal(isNewVideo("مرحبا"), false);
  assert.equal(isNewVideo("/start"), false);
});

test("unauthorized chat is ignored with zero outbound calls", async () => {
  const calls = installFetch();
  const response = await post(textUpdate("🎬 فيديو جديد", { chat: 999 }));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).ignored, true);
  assert.equal(calls.length, 0);
});

test("unauthorized user is ignored with zero outbound calls", async () => {
  const calls = installFetch();
  const response = await post(textUpdate("🎬 فيديو جديد", { user: 999 }));
  assert.equal((await response.json()).ignored, true);
  assert.equal(calls.length, 0);
});

test("wrong webhook secret is rejected with 403 and zero outbound calls", async () => {
  const calls = installFetch();
  const response = await post(textUpdate("🎬 فيديو جديد"), { secret: "nope" });
  assert.equal(response.status, 403);
  assert.equal(calls.length, 0);
});

test("unauthorized inline callback does not dispatch", async () => {
  const calls = installFetch();
  const update = callbackUpdate("publish:abc");
  update.callback_query.from.id = 999;
  await post(update);
  assert.equal(calls.length, 0);
});

for (const action of ["publish", "reject", "regenerate"]) {
  test(`inline callback ${action} still dispatches review.yml`, async () => {
    const calls = installFetch();
    const response = await post(callbackUpdate(`${action}:lan-tukmil-alqissa-20261018-x`));
    assert.equal(response.status, 200);
    const dispatches = github(calls);
    assert.equal(dispatches.length, 1);
    assert.ok(dispatches[0].url.endsWith("/workflows/review.yml/dispatches"));
    assert.deepEqual(dispatches[0].body.inputs, { action, identifier: "lan-tukmil-alqissa-20261018-x" });
    assert.equal(tg(calls, "answerCallbackQuery").length, 1);
  });
}

test("a failing Telegram send after a successful dispatch still returns 200 and dispatched once", async () => {
  const calls = installFetch({ telegramOk: false });
  const response = await post(textUpdate("🎬 فيديو جديد"));
  assert.equal(response.status, 200);
  assert.equal(github(calls).length, 1);
});

test("a failing Telegram send after an inline callback dispatch returns 200 and dispatched once", async () => {
  const calls = installFetch({ telegramOk: false });
  const response = await post(callbackUpdate("publish:abc"));
  assert.equal(response.status, 200);
  assert.equal(github(calls).length, 1);
});

test("dispatch failure returns 200, notifies best-effort and never dispatches twice", async () => {
  const calls = installFetch({ githubStatus: 500 });
  const response = await post(textUpdate("🎬 فيديو جديد"));
  assert.equal(response.status, 200);
  assert.equal(github(calls).length, 1);
  const sent = tg(calls, "sendMessage");
  assert.equal(sent.length, 1);
  assert.match(sent[0].body.text, /تعذر بدء الإجراء السحابي/);
});

test("GET health check is unchanged", async () => {
  installFetch();
  const response = await worker.fetch(new Request("https://worker.example/"), env);
  assert.deepEqual(await response.json(), { ok: true, service: "sleep-history-telegram" });
});
