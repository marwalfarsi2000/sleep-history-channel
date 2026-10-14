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

const CHILD_TTS_MODEL = "gemini-3.8-flash-lite-tts";
const CHILD_AUDIO_FOLDER = "أصوات الطفلة";
const MAX_VOICE_TEXT_CHARS = 8000;

function voiceText(text = "") {
  const trimmed = text.trim();
  for (const prefix of ["/voice", "/صوت", "صوت:", "صوت："]) {
    if (trimmed === prefix) return "";
    if (trimmed.startsWith(`${prefix} `) || trimmed.startsWith(`${prefix}\n`)) {
      return trimmed.slice(prefix.length).trim();
    }
  }
  return null;
}

function audioData(payload) {
  const direct = payload?.output_audio || payload?.outputAudio;
  if (direct?.data) return direct.data;
  for (const step of payload?.steps || []) {
    for (const content of step?.content || []) {
      if (content?.type === "audio" && content?.data) return content.data;
    }
  }
  throw new Error("Gemini returned no audio");
}

function decodeBase64(value) {
  const binary = atob(value);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  if (bytes.length < 1024 || String.fromCharCode(...bytes.slice(0, 4)) !== "RIFF") {
    throw new Error("Gemini returned invalid WAV audio");
  }
  return bytes;
}

async function synthesizeChildVoice(env, text) {
  const response = await fetch("https://generativelanguage.googleapis.com/v1beta/interactions", {
    method: "POST",
    headers: {
      "x-goog-api-key": env.GEMINI_API_KEY,
      "content-type": "application/json; charset=utf-8",
    },
    body: JSON.stringify({
      model: CHILD_TTS_MODEL,
      input: [{
        type: "user_input",
        content: [{
          type: "text",
          text,
          annotations: [{
            type: "speech_metadata",
            style: "لطيف وطبيعي وواضح، بعفوية طفولية هادئة، وبالعربية الفصحى دون مبالغة كرتونية.",
          }],
        }],
      }],
      response_format: { type: "audio" },
      generation_config: { speech_config: [{ voice: env.GEMINI_CHILD_VOICE_ID }] },
    }),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(`Gemini TTS failed: ${response.status}`);
  return decodeBase64(audioData(payload));
}

async function driveAccessToken(env) {
  const body = new URLSearchParams({
    client_id: env.GOOGLE_DRIVE_CLIENT_ID,
    client_secret: env.GOOGLE_DRIVE_CLIENT_SECRET,
    refresh_token: env.GOOGLE_DRIVE_REFRESH_TOKEN,
    grant_type: "refresh_token",
  });
  const response = await fetch("https://oauth2.googleapis.com/token", {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body,
  });
  const payload = await response.json();
  if (!response.ok || !payload.access_token) throw new Error(`Google OAuth failed: ${response.status}`);
  return payload.access_token;
}

async function driveFolder(token) {
  const escaped = CHILD_AUDIO_FOLDER.replaceAll("'", "\\'");
  const params = new URLSearchParams({
    q: `name = '${escaped}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false`,
    spaces: "drive",
    fields: "files(id,name)",
    pageSize: "10",
  });
  const headers = { authorization: `Bearer ${token}` };
  let response = await fetch(`https://www.googleapis.com/drive/v3/files?${params}`, { headers });
  let payload = await response.json();
  if (!response.ok) throw new Error(`Google Drive folder lookup failed: ${response.status}`);
  if (payload.files?.[0]?.id) return payload.files[0].id;
  response = await fetch("https://www.googleapis.com/drive/v3/files?fields=id", {
    method: "POST",
    headers: { ...headers, "content-type": "application/json; charset=utf-8" },
    body: JSON.stringify({ name: CHILD_AUDIO_FOLDER, mimeType: "application/vnd.google-apps.folder" }),
  });
  payload = await response.json();
  if (!response.ok || !payload.id) throw new Error(`Google Drive folder creation failed: ${response.status}`);
  return payload.id;
}

async function uploadVoiceAudio(env, audio) {
  const token = await driveAccessToken(env);
  const folderId = await driveFolder(token);
  const stamp = new Date().toISOString().replaceAll(":", "-").replace(".000Z", "Z");
  const metadata = { name: `child-voice-${stamp}.wav`, parents: [folderId] };
  const fields = encodeURIComponent("id,name,webViewLink");
  let response = await fetch(`https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&fields=${fields}`, {
    method: "POST",
    headers: {
      authorization: `Bearer ${token}`,
      "content-type": "application/json; charset=utf-8",
      "x-upload-content-type": "audio/wav",
    },
    body: JSON.stringify(metadata),
  });
  const uploadUrl = response.headers.get("location");
  if (!response.ok || !uploadUrl) throw new Error(`Google Drive upload initialization failed: ${response.status}`);
  response = await fetch(uploadUrl, {
    method: "PUT",
    headers: { "content-type": "audio/wav" },
    body: audio,
  });
  const result = await response.json();
  if (!response.ok || !result.id) throw new Error(`Google Drive upload failed: ${response.status}`);
  return result.webViewLink || `https://drive.google.com/file/d/${result.id}/view`;
}

async function createVoiceFile(env, chatId, text) {
  try {
    const audio = await synthesizeChildVoice(env, text);
    const link = await uploadVoiceAudio(env, audio);
    await telegram(env, "sendMessage", {
      chat_id: chatId,
      text: `تم إنشاء الصوت بصوت الطفلة ✅\nتحميله من Google Drive:\n${link}`,
      disable_web_page_preview: true,
    });
  } catch (error) {
    const stage = String(error?.message || "").startsWith("Gemini") ? "توليد الصوت" : "الرفع إلى Google Drive";
    await telegram(env, "sendMessage", {
      chat_id: chatId,
      text: `تعذر إكمال طلب الصوت عند مرحلة: ${stage}. لم تتأثر أتمتة الفيديو.`,
    });
  }
}

function isNewVideo(text = "") {
  const normalized = text.trim().toLowerCase().replace(/\s+/g, " ");
  return ["/new", "/video", "/episode", "فيديو جديد", "حلقة جديدة", "أنشئ فيديو جديد", "انشئ فيديو جديد"].includes(normalized);
}

export default {
  async fetch(request, env, ctx) {
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
      const requestedVoiceText = update.message ? voiceText(update.message.text) : null;
      if (requestedVoiceText !== null) {
        if (!requestedVoiceText) {
          await telegram(env, "sendMessage", {
            chat_id: chatId,
            text: "اكتبي النص بهذه الصيغة:\n/voice النص الذي تريدين تحويله إلى صوت\n\nويمكنك أيضًا كتابة: صوت: النص",
          });
        } else if (requestedVoiceText.length > MAX_VOICE_TEXT_CHARS) {
          await telegram(env, "sendMessage", {
            chat_id: chatId,
            text: `النص طويل جدًا. الحد الحالي ${MAX_VOICE_TEXT_CHARS} حرف في كل ملف صوتي.`,
          });
        } else {
          await telegram(env, "sendMessage", {
            chat_id: chatId,
            text: "تم استلام النص ✅ جارٍ إنشاء الصوت ورفعه إلى Google Drive.",
          });
          ctx.waitUntil(createVoiceFile(env, chatId, requestedVoiceText));
        }
      } else if (update.message && isNewVideo(update.message.text)) {
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

