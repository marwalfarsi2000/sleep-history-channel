const json = (value, status = 200) => new Response(JSON.stringify(value), {
  status,
  headers: { "content-type": "application/json; charset=utf-8" },
});

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

function isNewVideo(text = "") {
  const normalized = text.trim().toLowerCase().replace(/\s+/g, " ");
  return ["/new", "/video", "/episode", "فيديو جديد", "حلقة جديدة", "أنشئ فيديو جديد", "انشئ فيديو جديد"].includes(normalized);
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
      if (update.message && isNewVideo(update.message.text)) {
        await dispatch(env, "production.yml", { trigger_source: "telegram" });
        await telegram(env, "sendMessage", {
          chat_id: chatId,
          text: "تم استلام طلب فيديو جديد ✅\nبدأ التشغيل السحابي، وسيصل رابط Internet Archive عند اكتماله للمراجعة.",
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
        await telegram(env, "answerCallbackQuery", { callback_query_id: callback.id, text: labels[action] });
        await telegram(env, "sendMessage", { chat_id: chatId, text: labels[action] });
      }
      return json({ ok: true });
    } catch (error) {
      await telegram(env, "sendMessage", {
        chat_id: chatId,
        text: "تعذر بدء الإجراء السحابي. لم يتم تنفيذ نشر أو حذف. سيبقى الفيديو في حالته الحالية.",
      });
      return json({ ok: false, error: error.message }, 500);
    }
  },
};

