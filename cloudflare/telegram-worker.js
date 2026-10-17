const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { "content-type": "application/json; charset=utf-8" },
});

export const NEW_VIDEO_BUTTON = "🎬 فيديو جديد";

export const MAIN_KEYBOARD = {
  keyboard: [[{ text: NEW_VIDEO_BUTTON }]],
  resize_keyboard: true,
  is_persistent: true,
};

async function telegram(env, method, payload) {
  const response = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const result = await response.json();
  if (!response.ok || !result.ok) throw new Error(`Telegram ${method} failed`);
  return result;
}

async function tryTelegram(env, method, payload) {
  try {
    await telegram(env, method, payload);
  } catch (_error) {
    // A failed notification must never turn into a non-200 response: Telegram
    // would redeliver the update and the workflow could be dispatched twice.
  }
}

async function dispatch(env, workflow, inputs = {}) {
  const response = await fetch(
    `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/actions/workflows/${workflow}/dispatches`,
    {
      method: "POST",
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "x-github-api-version": "2022-11-28",
        "user-agent": "sleep-history-telegram-worker",
        "content-type": "application/json",
      },
      body: JSON.stringify({ ref: "main", inputs }),
    },
  );
  if (response.status !== 204) throw new Error(`GitHub dispatch failed: ${response.status}`);
}

const NEW_VIDEO_COMMANDS = [
  "/new", "/video", "/episode", "فيديو جديد", "حلقة جديدة", "أنشئ فيديو جديد", "انشئ فيديو جديد",
];

function normalize(text = "") {
  return String(text).trim().toLowerCase().replace(/\s+/g, " ");
}

export function isNewVideo(text = "") {
  const normalized = normalize(text);
  const accepted = [...NEW_VIDEO_COMMANDS, NEW_VIDEO_BUTTON].map(normalize);
  if (accepted.includes(normalized)) return true;
  return normalized.replace(/^🎬\s*/u, "") === normalize("فيديو جديد");
}

function isStart(text = "") {
  return /^\/start(@\w+)?(\s|$)/i.test(String(text).trim());
}

export default {
  async fetch(request, env) {
    if (request.method === "GET") return json({ ok: true, service: "sleep-history-telegram" });
    if (request.method !== "POST") return json({ ok: false }, 405);
    if (request.headers.get("x-telegram-bot-api-secret-token") !== env.TELEGRAM_WEBHOOK_SECRET) {
      return json({ ok: false }, 403);
    }
    const update = await request.json();
    const callback = update.callback_query;
    const message = update.message || callback?.message;
    const chatId = String(message?.chat?.id || "");
    const userId = String((update.message?.from || callback?.from)?.id || "");
    if (chatId !== String(env.TELEGRAM_CHAT_ID) || userId !== String(env.TELEGRAM_USER_ID)) {
      return json({ ok: true, ignored: true });
    }
    try {
      if (update.message && isStart(update.message.text)) {
        await tryTelegram(env, "sendMessage", {
          chat_id: chatId,
          text: "أهلًا بك 🌙\nاضغطي «🎬 فيديو جديد» أسفل الشاشة لبدء حلقة جديدة.",
          reply_markup: MAIN_KEYBOARD,
        });
      } else if (update.message && isNewVideo(update.message.text)) {
        try {
          await dispatch(env, "production.yml", { trigger_source: "telegram" });
        } catch (_error) {
          await tryTelegram(env, "sendMessage", {
            chat_id: chatId,
            text: "تعذر بدء الإجراء السحابي. لم يتم تنفيذ نشر أو حذف. سيبقى الفيديو في حالته الحالية.",
            reply_markup: MAIN_KEYBOARD,
          });
          return json({ ok: false, dispatched: false });
        }
        await tryTelegram(env, "sendMessage", {
          chat_id: chatId,
          text: "تم استلام طلب فيديو جديد ✅\nبدأ التشغيل السحابي، وسيصل رابط Internet Archive عند اكتماله للمراجعة.",
          reply_markup: MAIN_KEYBOARD,
        });
      } else if (callback) {
        const separator = callback.data.indexOf(":");
        const action = callback.data.slice(0, separator);
        const identifier = callback.data.slice(separator + 1);
        if (!["publish", "reject", "regenerate"].includes(action) || !identifier) throw new Error("Unknown action");
        await dispatch(env, "review.yml", { action, identifier });
        const labels = {
          publish: "بدأ النشر إلى YouTube.",
          reject: "بدأ حذف الحلقة المرفوضة.",
          regenerate: "بدأت إعادة توليد الحلقة.",
        };
        await tryTelegram(env, "answerCallbackQuery", { callback_query_id: callback.id, text: labels[action] });
        await tryTelegram(env, "sendMessage", { chat_id: chatId, text: labels[action] });
      }
      return json({ ok: true });
    } catch (error) {
      await tryTelegram(env, "sendMessage", {
        chat_id: chatId,
        text: "تعذر بدء الإجراء السحابي. لم يتم تنفيذ نشر أو حذف. سيبقى الفيديو في حالته الحالية.",
      });
      return json({ ok: false, error: error.message });
    }
  },
};
