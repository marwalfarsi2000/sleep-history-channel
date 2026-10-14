from __future__ import annotations

import argparse, asyncio, base64, hashlib, html, json, os, re, shutil, subprocess, sys, time
import urllib.error
import urllib.request
import difflib
from pathlib import Path
from PIL import Image
import edge_tts
from generate_daily import gemini

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text(encoding="utf-8"))

def load_secrets() -> dict[str, str]:
    values = dict(os.environ)
    path = Path(os.environ.get(
        "SLEEP_HISTORY_SECRETS_FILE",
        str(Path.home() / "AppData/Local/SleepHistoryAutomation/secrets.env"),
    ))
    if path.exists():
        for raw in path.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if not values.get(key):
                values[key] = value.strip()
    return values

SECRETS = load_secrets()

def run(cmd: list[str], check=True):
    print("RUN", " ".join(map(str, cmd)))
    # Keep normal ffmpeg diagnostics away from Windows PowerShell 5.1 stderr.
    # With ErrorActionPreference=Stop, PowerShell otherwise aborts the parent
    # automation on ffmpeg's harmless version/progress output.
    result = subprocess.run(cmd, check=False, text=True, capture_output=True)
    if result.returncode != 0:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print(result.stderr)
        if check:
            raise subprocess.CalledProcessError(
                result.returncode,
                cmd,
                output=result.stdout,
                stderr=result.stderr,
            )
    return result

def ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        candidates = list(Path.home().glob("AppData/Local/Microsoft/WinGet/Packages/Gyan.FFmpeg.Essentials*/ffmpeg-*/bin/ffmpeg.exe"))
        if candidates:
            return str(candidates[0])
        raise RuntimeError("ffmpeg not found")
    return found

def ffprobe() -> str:
    exe = Path(ffmpeg()).with_name("ffprobe.exe")
    return str(exe) if exe.exists() else "ffprobe"

def duration(path: Path) -> float:
    p = subprocess.run([ffprobe(), "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], capture_output=True, text=True, check=True)
    return float(p.stdout.strip())

def arabic_words(text: str) -> int:
    return len(re.findall(r"[\u0600-\u06FF]+", text))

ARABIC_DIRECTION_TAGS = {
    "[بِنَبْرَةٍ هادِئَةٍ وَدافِئَةٍ]": "[soft]",
    "[بِهَمْسٍ خَفِيفٍ]": "[near-whisper]",
    "[بِدِفْءٍ وَتَأَمُّلٍ]": "[warm]",
    "[بِتَأْكيدٍ هادِئٍ]": "[emphasis]",
    "[بِحَماسٍ خَفِيفٍ]": "[excited]",
    "[بِأَسًى هادِئٍ]": "[sad]",
    "[بِاسْتِهْزاءٍ خَفِيفٍ]": "[sarcastic]",
    "[وَقْفَةٌ طَوِيلَةٌ]": "[long pause]",
}
DIACRITICS_RE = re.compile(r"[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]")
DIRECTION_RE = re.compile(r"\[[^\]\n]{1,100}\]")
DIACRITIZATION_VERSION = "full-v6-double-reviewed-pausal-taa-marbuta"
TTS_AUDIO_CACHE_VERSION = "native-speed-1.0-v2"
_DIACRITIZATION_LAST_REQUEST_AT = 0.0
MALFORMED_DIACRITIC_PATTERNS = (
    re.compile(r"الِا"),
    re.compile(r"[َُِّْ]{3,}"),
)

def without_direction_tags(text: str) -> str:
    return DIRECTION_RE.sub("", text)

def base_signature(text: str) -> str:
    text = without_direction_tags(text)
    text = DIACRITICS_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()

def lexical_fidelity(source: str, candidate: str) -> tuple[bool, list[tuple[str, str]]]:
    token_re = re.compile(r"[\u0621-\u064A]+|[A-Za-z]+|\d+(?:[.,]\d+)?")
    source_tokens = token_re.findall(DIACRITICS_RE.sub("", without_direction_tags(source)))
    candidate_tokens = token_re.findall(DIACRITICS_RE.sub("", without_direction_tags(candidate)))
    if len(source_tokens) != len(candidate_tokens):
        return False, [("TOKEN_COUNT", f"{len(source_tokens)}->{len(candidate_tokens)}")]
    corrections = []
    for original, revised in zip(source_tokens, candidate_tokens):
        if original == revised:
            continue
        both_arabic = bool(re.fullmatch(r"[\u0621-\u064A]+", original)) and bool(
            re.fullmatch(r"[\u0621-\u064A]+", revised)
        )
        similarity = difflib.SequenceMatcher(None, original, revised).ratio()
        if not both_arabic or abs(len(original) - len(revised)) > 1 or similarity < 0.78:
            return False, [(original, revised)]
        corrections.append((original, revised))
    allowed = max(2, int(len(source_tokens) * 0.02))
    return len(corrections) <= allowed, corrections

def restore_source_lexemes(source: str, candidate: str) -> str:
    """Keep Gemini's marks while mechanically restoring every original letter."""
    word_pattern = re.compile(r"(?:[\u0621-\u064A][\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]*)+")
    source_words = list(word_pattern.finditer(without_direction_tags(source)))
    candidate_words = list(word_pattern.finditer(without_direction_tags(candidate)))
    if len(source_words) != len(candidate_words):
        return candidate

    replacements = []
    for original_match, candidate_match in zip(source_words, candidate_words):
        original = DIACRITICS_RE.sub("", original_match.group())
        vocalized = candidate_match.group()
        revised = DIACRITICS_RE.sub("", vocalized)
        if original == revised:
            continue
        # A final alif is required when Gemini correctly adds tanween al-fath
        # to an originally unvocalized accusative word (e.g. وزن -> وَزْنًا).
        if revised == original + "ا" and "\u064b" in vocalized:
            continue
        graphemes = []
        for char in vocalized:
            if DIACRITICS_RE.fullmatch(char) and graphemes:
                graphemes[-1] += char
            elif not DIACRITICS_RE.fullmatch(char):
                graphemes.append(char)
        rebuilt = list(original)
        matcher = difflib.SequenceMatcher(None, original, revised)
        for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
            if opcode == "equal":
                for source_index, candidate_index in zip(range(i1, i2), range(j1, j2)):
                    marks = "".join(DIACRITICS_RE.findall(graphemes[candidate_index]))
                    rebuilt[source_index] = original[source_index] + marks
            elif opcode == "replace" and (i2 - i1) == (j2 - j1):
                for source_index, candidate_index in zip(range(i1, i2), range(j1, j2)):
                    marks = "".join(DIACRITICS_RE.findall(graphemes[candidate_index]))
                    rebuilt[source_index] = original[source_index] + marks
        replacements.append((candidate_match.start(), candidate_match.end(), "".join(rebuilt)))
    for start, end, replacement in reversed(replacements):
        candidate = candidate[:start] + replacement + candidate[end:]
    return candidate

def diacritic_ratio(text: str) -> float:
    text = without_direction_tags(text)
    letters = len(re.findall(r"[ء-ي]", text))
    marks = len(DIACRITICS_RE.findall(text))
    return marks / max(letters, 1)

def diacritization_quality_errors(text: str) -> list[str]:
    """Catch deterministic defects before expensive TTS is allowed to run."""
    errors = []
    bare_tags_removed = without_direction_tags(text)
    for pattern in MALFORMED_DIACRITIC_PATTERNS:
        match = pattern.search(bare_tags_removed)
        if match:
            errors.append(f"malformed sequence: {match.group(0)}")
    if diacritic_ratio(bare_tags_removed) < 0.27:
        errors.append("insufficient diacritic coverage")
    return errors

def normalize_arabic_diacritics(text: str) -> str:
    """Apply safe mark-only corrections without changing any Arabic letters."""
    # With the definite article before hamzat al-wasl, the kasra belongs on the
    # following alif (الاِزْدِهَار), not on the lam (الِازْدِهَار).
    text = text.replace("الِا", "الاِ")
    # Collapse accidentally duplicated identical marks emitted by the model.
    text = re.sub(r"([َُِّْ])\1+", r"\1", text)
    return text

def merge_reviewed_diacritics(primary: str, reviewed: str) -> str:
    """Keep second-pass corrections without allowing it to drop first-pass marks."""
    def graphemes(value: str) -> list[str]:
        result: list[str] = []
        for char in value:
            if DIACRITICS_RE.fullmatch(char) and result:
                result[-1] += char
            else:
                result.append(char)
        return result

    primary_graphemes = graphemes(primary)
    reviewed_graphemes = graphemes(reviewed)
    primary_base = "".join(item[0] for item in primary_graphemes)
    reviewed_base = "".join(item[0] for item in reviewed_graphemes)
    if primary_base != reviewed_base:
        return reviewed
    merged = []
    for original, correction in zip(primary_graphemes, reviewed_graphemes):
        base = correction[0]
        correction_marks = "".join(DIACRITICS_RE.findall(correction[1:]))
        original_marks = "".join(DIACRITICS_RE.findall(original[1:]))
        if re.fullmatch(r"[ء-ي]", base) and not correction_marks:
            merged.append(base + original_marks)
        else:
            merged.append(base + correction_marks)
    return "".join(merged)

def gemini_diacritics(prompt: str, block_index: int) -> str:
    """Rate-limited deterministic Gemini call used only for Arabic proofreading."""
    global _DIACRITIZATION_LAST_REQUEST_AT
    wait_seconds = 4.2 - (time.monotonic() - _DIACRITIZATION_LAST_REQUEST_AT)
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    try:
        candidate, _ = gemini(prompt, max_tokens=8192, temperature=0.0)
    finally:
        _DIACRITIZATION_LAST_REQUEST_AT = time.monotonic()
    candidate = re.sub(r"^```(?:arabic|text)?\s*|\s*```$", "", candidate.strip(), flags=re.I | re.S)
    if not candidate:
        raise RuntimeError(f"empty Arabic proofreading response in block {block_index}")
    return candidate

def split_diacritization_blocks(text: str, limit: int = 1200) -> list[str]:
    """Return small, paragraph-aligned units that Gemini can preserve exactly."""
    pieces = []
    for paragraph in (part.strip() for part in re.split(r"\n\s*\n", text)):
        if not paragraph:
            continue
        if len(paragraph) <= limit:
            pieces.append(paragraph)
            continue
        sentences = [part.strip() for part in re.split(r"(?<=[.!؟؛])\s+", paragraph) if part.strip()]
        current = ""
        for sentence in sentences:
            if len(sentence) > limit:
                # A pathological sentence is split only at Arabic comma boundaries.
                clauses = [part.strip() for part in re.split(r"(?<=،)\s+", sentence) if part.strip()]
            else:
                clauses = [sentence]
            for clause in clauses:
                candidate = f"{current} {clause}".strip() if current else clause
                if current and len(candidate) > limit:
                    pieces.append(current)
                    current = clause
                else:
                    current = candidate
        if current:
            pieces.append(current)
    blocks, current = [], ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}".strip() if current else piece
        if current and len(candidate) > limit:
            blocks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        blocks.append(current)
    return blocks

def direction_tag(text: str, index: int) -> str:
    """Choose a restrained sleep-narration direction without changing story text."""
    bare = DIACRITICS_RE.sub("", text)
    if re.search(r"ليل|سكون|صمت|هدوء|قمر|نجوم|ينام|النوم", bare):
        return "[بِهَمْسٍ خَفِيفٍ]"
    if re.search(r"حزن|أسى|انهار|انهيار|دمار|مات|وفاة|اختفى", bare):
        return "[بِأَسًى هادِئٍ]"
    if index % 9 == 0 and re.search(r"سوق|تجارة|قافلة|سفينة|ميناء|مرفأ|رحلة|بناء", bare):
        return "[بِحَماسٍ خَفِيفٍ]"
    if re.search(r"لكن|غير أن|الأهم|من المهم|ومع ذلك", bare):
        return "[بِتَأْكيدٍ هادِئٍ]"
    return "[بِدِفْءٍ وَتَأَمُّلٍ]" if index % 2 else "[بِنَبْرَةٍ هادِئَةٍ وَدافِئَةٍ]"

def diacritize_and_annotate(clean_story: str, cache_dir: Path | None = None, limit: int = 1200) -> str:
    global _DIACRITIZATION_LAST_REQUEST_AT
    outputs = []
    if cache_dir:
        cache_dir.mkdir(parents=True, exist_ok=True)
    for index, block in enumerate(split_diacritization_blocks(clean_story, limit=limit), 1):
        block_hash = hashlib.sha256(f"{DIACRITIZATION_VERSION}\n{block}".encode("utf-8")).hexdigest()
        cache_path = cache_dir / f"{block_hash}.txt" if cache_dir else None
        candidate = cache_path.read_text(encoding="utf-8").strip() if cache_path and cache_path.exists() else ""
        if candidate:
            candidate = normalize_arabic_diacritics(restore_source_lexemes(block, candidate))
        cached_ok, _ = lexical_fidelity(block, candidate) if candidate else (False, [])
        if cached_ok and not diacritization_quality_errors(candidate) and not DIRECTION_RE.findall(candidate):
            outputs.append(f"{direction_tag(block, index)}\n{candidate}")
            continue
        prompt = f"""أنت مدقق لغوي عربي متخصص في النصوص المهيأة للتحويل إلى صوت عربي فصيح.

المطلوب:
1. أعد النص نفسه حرفيًا دون حذف أو إضافة أو استبدال أي كلمة أو رقم أو علامة ترقيم.
2. ضع التشكيل العربي الكامل الصحيح: الفتحة والضمة والكسرة والسكون والشدة والتنوين حيث يلزم.
3. راعِ السياق النحوي والدلالي، وثبّت نطق أسماء الأشخاص والمدن والمصطلحات التاريخية بصورة متسقة.
4. استخدم الوقف العربي الطبيعي في نهاية الجمل؛ لا تخمّن حركة إعرابية أخيرة عند موضع الوقف.
5. في كلمات مثل «الازدهار» و«الاستقرار»، ضع الكسرة على ألف الوصل هكذا «الاِزْدِهَار» و«الاِسْتِقْرَار»، ولا تكتب «الِازدهار».
6. لا تضف أي وسم أداء أو عنوان أو شرح أو Markdown أو أقواس جديدة.
7. لا تصحح الإملاء ولا الأسلوب؛ أضف الحركات فقط إلى الحروف العربية الموجودة.

النص:
{block}
"""
        last_error = ""
        for attempt in range(3):
            try:
                candidate = gemini_diacritics(prompt, index)
            except RuntimeError as exc:
                last_error = f"temporary Gemini failure in block {index}: {exc}"
                if attempt < 2:
                    time.sleep(4.2)
                    continue
                candidate = ""
            if not candidate:
                continue
            candidate = normalize_arabic_diacritics(restore_source_lexemes(block, candidate))
            faithful, corrections = lexical_fidelity(block, candidate)
            if not faithful:
                last_error = f"lexical content changed in block {index}"
                prompt += "\nتنبيه صارم: المحاولة السابقة غيّرت النص. لا تغير أي حرف أصلي؛ أضف الحركات ووسم الأداء فقط."
                continue
            if DIRECTION_RE.findall(candidate):
                last_error = f"unexpected direction tag in block {index}"
                continue
            quality_errors = diacritization_quality_errors(candidate)
            if quality_errors:
                last_error = f"Arabic diacritization defects in block {index}: {quality_errors}"
                continue
            review_prompt = f"""أنت المراجع اللغوي الثاني لنص عربي سيُنطق آليًا بصوت هادئ.

راجع التشكيل في النسخة المرشحة مقابل النص الأصلي، وصحح الحركات الخاطئة فقط.
- لا تحذف أو تضف أو تستبدل أي حرف أو كلمة أو رقم أو علامة ترقيم.
- راجع الأسماء التاريخية والجغرافية، الشدات، همزات الوصل والقطع، وبنية الكلمات في سياق الجملة.
- اجعل أواخر الجمل ملائمة للوقف الصوتي الطبيعي، ولا تفرض حركة إعراب غير مؤكدة عند الوقف.
- لا تضف أقواسًا أو وسوم أداء أو شروحًا أو Markdown.
- أخرج النص المصحح فقط.

النص الأصلي غير المشكول:
{block}

النسخة المرشحة:
{candidate}
"""
            try:
                reviewed = gemini_diacritics(review_prompt, index)
            except RuntimeError as exc:
                last_error = f"second Arabic review failed in block {index}: {exc}"
                if attempt < 2:
                    continue
                reviewed = ""
            if not reviewed:
                continue
            reviewed = normalize_arabic_diacritics(restore_source_lexemes(block, reviewed))
            reviewed = normalize_arabic_diacritics(merge_reviewed_diacritics(candidate, reviewed))
            reviewed_faithful, _ = lexical_fidelity(block, reviewed)
            reviewed_errors = diacritization_quality_errors(reviewed)
            if not reviewed_faithful or DIRECTION_RE.findall(reviewed) or reviewed_errors:
                last_error = f"second Arabic review rejected in block {index}: {reviewed_errors}"
                if attempt < 2:
                    continue
                # The first pass has already passed lexical, coverage, and
                # malformed-mark guards. Never let an unsafe second-pass
                # rewrite stop the whole daily workflow or corrupt the text.
                reviewed = candidate
            candidate = reviewed
            if cache_path:
                cache_path.write_text(candidate + "\n", encoding="utf-8")
            outputs.append(f"{direction_tag(block, index)}\n{candidate}")
            break
        else:
            smaller = split_diacritization_blocks(block, limit=max(320, len(block) // 2))
            if len(smaller) > 1:
                # Retry only the difficult unit at finer sentence boundaries.
                outputs.append(diacritize_and_annotate(block, cache_dir, limit=max(320, len(block) // 2)))
                continue
            raise RuntimeError(f"Arabic diacritization validation failed: {last_error}")
    annotated = "\n\n".join(outputs).strip()
    faithful, _ = lexical_fidelity(clean_story, annotated)
    if not faithful:
        raise RuntimeError("Arabic diacritization changed the source story")
    return annotated

def api_text(text: str) -> str:
    # Keep expressive cues in the human review copy, but never send unverified
    # bracket tags to Fish: some voices pronounce them literally.
    text = without_direction_tags(text)
    # English glosses inside Arabic narration commonly get pronounced abruptly.
    text = re.sub(r"\s*\([A-Za-z][^()]{0,120}\)", "", text)
    # Fish can pronounce a fully inflected final taa marbuta as /t/ at a stop
    # (for example عَمِيقَةً -> "عميقت").  In Arabic pausal reading the final
    # short case ending/tanween is dropped, so keep the internal diacritics but
    # remove only the marks attached to a sentence-final taa marbuta.
    text = re.sub(
        r"ة[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]*(?=\s*[.!؟…؛]|\s*$)",
        "ة",
        text,
    )
    # Fish respects punctuation and paragraph boundaries. A blank line after a
    # completed sentence creates a short natural pause without unsupported SSML.
    text = re.sub(r"\s*([.!؟])\s*", r"\1\n\n", text)
    text = re.sub(r"\s*(؛)\s*", r"\1\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def split_chunks(text: str, limit: int = 2600) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.lstrip().startswith("#")]
    out, current = [], ""
    for paragraph in paragraphs:
        sentences = [s.strip() for s in re.split(r"(?<=[.!؟؛])\s+", paragraph) if s.strip()]
        for sentence in sentences:
            separator = "\n\n" if current else ""
            if current and len(current) + len(sentence) + len(separator) > limit:
                out.append(current.strip()); current = ""
                separator = ""
            current += separator + sentence
    if current.strip(): out.append(current.strip())
    return out

async def edge_tts_one(text: str, out: Path):
    text = re.sub(r"\[[a-z -]+\]\s*", "", text)
    communicate = edge_tts.Communicate(text, SETTINGS["tts_voice"], rate=SETTINGS["tts_rate"], volume="-5%")
    await communicate.save(str(out))

def fish_tts_one(text: str, out: Path):
    api_key = SECRETS.get("FISH_API_KEY", "")
    voice_id = SECRETS.get("FISH_VOICE_ID", "")
    if not api_key or not voice_id:
        raise RuntimeError("Fish Audio is selected but FISH_API_KEY or FISH_VOICE_ID is missing")
    generation_speed = float(SETTINGS.get("tts_generation_speed", 1.0))
    if generation_speed != 1.0:
        raise RuntimeError(
            f"Fish generation speed guard failed: expected normal 1.0, got {generation_speed}"
        )
    # Deliberately omit Fish's speed control.  The narration is generated at
    # the voice's native cadence, then slowed deterministically with ffmpeg.
    payload = json.dumps(
        {"text": text, "reference_id": voice_id, "format": "mp3"},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.fish.audio/v1/tts",
        data=payload,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "model": SETTINGS.get("fish_model", "s2.1-pro-free"),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            out.write_bytes(response.read())
    except urllib.error.HTTPError as exc:
        if exc.code in (402, 429):
            raise RuntimeError(f"BLOCKED_COST_OR_FREE_LIMIT: Fish Audio returned HTTP {exc.code}") from exc
        raise RuntimeError(f"Fish Audio TTS failed with HTTP {exc.code}") from exc

def validate_fish_voice() -> None:
    api_key = SECRETS.get("FISH_API_KEY", "")
    voice_id = SECRETS.get("FISH_VOICE_ID", "")
    expected_name = SETTINGS.get("fish_voice_name", "ناشونال جيوغرافيك").strip()
    if not api_key or not voice_id:
        raise RuntimeError("Fish Audio is selected but FISH_API_KEY or FISH_VOICE_ID is missing")
    request = urllib.request.Request(
        f"https://api.fish.audio/model/{voice_id}",
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            model = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Fish voice verification failed with HTTP {exc.code}") from exc
    actual_name = str(model.get("title") or "").strip()
    if actual_name.casefold() != expected_name.casefold():
        raise RuntimeError(
            f"Fish voice mismatch: configured reference is '{actual_name or 'unknown'}', expected '{expected_name}'"
        )

def munsit_voices() -> list[dict]:
    api_key = SECRETS.get("MUNSIT_API_KEY", "")
    if not api_key:
        raise RuntimeError("Munsit is selected but MUNSIT_API_KEY is missing")
    request = urllib.request.Request(
        "https://api.munsit.com/api/v1/voices",
        headers={"x-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(f"Munsit authentication failed with HTTP {exc.code}") from exc
        raise RuntimeError(f"Munsit voice verification failed with HTTP {exc.code}") from exc
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for field in ("data", "voices", "result"):
            if isinstance(payload.get(field), list):
                return payload[field]
    raise RuntimeError("Munsit returned an invalid voice-list response")

def validate_munsit_voice() -> None:
    voice_id = SECRETS.get("MUNSIT_VOICE_ID", "")
    expected_name = SETTINGS.get("munsit_voice_name", "جاد").strip()
    if not voice_id:
        raise RuntimeError("Munsit is selected but MUNSIT_VOICE_ID is missing")
    selected = next(
        (voice for voice in munsit_voices() if str(voice.get("voice_id") or "").strip() == voice_id),
        None,
    )
    if selected is None:
        raise RuntimeError("Configured Munsit voice_id is not present in the voice catalogue")
    actual_name = str(selected.get("name") or "").strip()
    if actual_name.casefold() != expected_name.casefold():
        raise RuntimeError(
            f"Munsit voice mismatch: configured voice is '{actual_name or 'unknown'}', expected '{expected_name}'"
        )

def munsit_tts_one(text: str, out: Path):
    api_key = SECRETS.get("MUNSIT_API_KEY", "")
    voice_id = SECRETS.get("MUNSIT_VOICE_ID", "")
    if not api_key or not voice_id:
        raise RuntimeError("Munsit is selected but MUNSIT_API_KEY or MUNSIT_VOICE_ID is missing")
    generation_speed = float(SETTINGS.get("tts_generation_speed", 1.0))
    if generation_speed != 1.0:
        raise RuntimeError(
            f"Munsit generation speed guard failed: expected normal 1.0, got {generation_speed}"
        )
    model = SETTINGS.get("munsit_model", "faseeh-v1-preview")
    payload = json.dumps({
        "voice_id": voice_id,
        "text": text,
        "stability": float(SETTINGS.get("munsit_stability", 0.5)),
        "speed": 1.0,
        "streaming": False,
        "sample_rate": int(SETTINGS.get("munsit_sample_rate", 48000)),
        "dialect": SETTINGS.get("munsit_dialect", "fusha"),
    }, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.munsit.com/api/v1/text-to-speech/{model}",
        data=payload,
        method="POST",
        headers={"x-api-key": api_key, "Content-Type": "application/json", "Accept": "audio/wav"},
    )
    wav_path = out.with_suffix(".munsit.wav")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            audio = response.read()
        if "audio" not in content_type or len(audio) <= 1024:
            raise RuntimeError("Munsit returned an invalid or empty audio response")
        wav_path.write_bytes(audio)
        run([ffmpeg(), "-y", "-i", str(wav_path), "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
    except urllib.error.HTTPError as exc:
        if exc.code in (402, 429):
            raise RuntimeError(f"BLOCKED_COST_OR_FREE_LIMIT: Munsit returned HTTP {exc.code}") from exc
        if exc.code in (401, 403):
            raise RuntimeError(f"Munsit authentication failed with HTTP {exc.code}") from exc
        raise RuntimeError(f"Munsit TTS failed with HTTP {exc.code}") from exc
    finally:
        wav_path.unlink(missing_ok=True)

def gemini_voice() -> dict:
    api_key = SECRETS.get("GEMINI_API_KEY", "")
    voice_id = SECRETS.get("GEMINI_TTS_VOICE_ID", "")
    if not api_key or not voice_id:
        raise RuntimeError("Gemini TTS is selected but GEMINI_API_KEY or GEMINI_TTS_VOICE_ID is missing")
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/voices/{voice_id}",
        headers={"x-goog-api-key": api_key, "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise RuntimeError(f"Gemini TTS authentication failed with HTTP {exc.code}") from exc
        raise RuntimeError(f"Gemini voice verification failed with HTTP {exc.code}") from exc

def validate_gemini_voice() -> None:
    expected_name = SETTINGS.get("gemini_tts_voice_name", "راوي القصص 1").strip()
    voice = gemini_voice()
    actual_name = str(voice.get("display_name") or voice.get("displayName") or "").strip()
    if actual_name != expected_name:
        raise RuntimeError(
            f"Gemini voice mismatch: configured voice is '{actual_name or 'unknown'}', expected '{expected_name}'"
        )

def _gemini_audio_data(payload: dict) -> str:
    direct = payload.get("output_audio") or payload.get("outputAudio") or {}
    if isinstance(direct, dict) and direct.get("data"):
        return str(direct["data"])
    for step in payload.get("steps") or []:
        for content in step.get("content") or []:
            if content.get("type") == "audio" and content.get("data"):
                return str(content["data"])
    raise RuntimeError("Gemini TTS returned no audio data")

GEMINI_TTS_RETRY_DELAYS = (60, 120, 300)


def _gemini_tts_quota_kind(body: str) -> str:
    normalized = body.lower()
    if "perday" in normalized or "per_day" in normalized or "requests per day" in normalized:
        return "daily"
    if "perminute" in normalized or "per_minute" in normalized or "requests per minute" in normalized:
        return "minute"
    if "tokensperminute" in normalized or "tokens_per_minute" in normalized:
        return "minute"
    return "unknown"


def _gemini_tts_models() -> list[str]:
    primary = str(SETTINGS.get("gemini_tts_model", "gemini-3.8-flash-tts")).strip()
    configured = SETTINGS.get("gemini_tts_fallback_models", [])
    if not isinstance(configured, list):
        raise RuntimeError("gemini_tts_fallback_models must be a JSON list")
    models = [primary, *(str(model).strip() for model in configured)]
    return list(dict.fromkeys(model for model in models if model))


def gemini_tts_one(text: str, out: Path):
    api_key = SECRETS.get("GEMINI_API_KEY", "")
    voice_id = SECRETS.get("GEMINI_TTS_VOICE_ID", "")
    if not api_key or not voice_id:
        raise RuntimeError("Gemini TTS is selected but GEMINI_API_KEY or GEMINI_TTS_VOICE_ID is missing")
    generation_speed = float(SETTINGS.get("tts_generation_speed", 1.0))
    if generation_speed != 1.0:
        raise RuntimeError(
            f"Gemini generation speed guard failed: expected normal 1.0, got {generation_speed}"
        )
    wav_path = out.with_suffix(".gemini.wav")
    try:
        result = None
        exhausted_models = []
        models = _gemini_tts_models()
        for model_index, model in enumerate(models):
            payload = json.dumps({
                "model": model,
                "input": [{
                    "type": "user_input",
                    "content": [{
                        "type": "text",
                        "text": text,
                        "annotations": [{
                            "type": "speech_metadata",
                            "style": SETTINGS.get("gemini_tts_style", "Calm Arabic historical sleep narration."),
                        }],
                    }],
                }],
                "response_format": {"type": "audio"},
                "generation_config": {"speech_config": [{"voice": voice_id}]},
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                "https://generativelanguage.googleapis.com/v1beta/interactions",
                data=payload,
                method="POST",
                headers={
                    "x-goog-api-key": api_key,
                    "Content-Type": "application/json; charset=utf-8",
                    "Accept": "application/json",
                },
            )
            for attempt in range(len(GEMINI_TTS_RETRY_DELAYS) + 1):
                try:
                    with urllib.request.urlopen(request, timeout=240) as response:
                        result = json.loads(response.read().decode("utf-8"))
                    break
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")
                    if exc.code == 402:
                        raise RuntimeError("BLOCKED_COST_OR_FREE_LIMIT: Gemini TTS payment required") from exc
                    if exc.code == 429:
                        quota_kind = _gemini_tts_quota_kind(body)
                        if quota_kind == "daily":
                            exhausted_models.append(model)
                            if model_index + 1 < len(models):
                                print(
                                    f"Gemini TTS daily free quota exhausted for {model}; "
                                    f"switching to free fallback {models[model_index + 1]}",
                                    flush=True,
                                )
                                break
                            raise RuntimeError(
                                "BLOCKED_COST_OR_FREE_LIMIT: Gemini TTS daily free quota exhausted "
                                f"for all configured models ({', '.join(exhausted_models)}); "
                                "saved chunks will resume after the Pacific-time reset"
                            ) from exc
                        if attempt < len(GEMINI_TTS_RETRY_DELAYS):
                            retry_after = exc.headers.get("Retry-After") if exc.headers else None
                            try:
                                delay = max(GEMINI_TTS_RETRY_DELAYS[attempt], int(retry_after or 0))
                            except ValueError:
                                delay = GEMINI_TTS_RETRY_DELAYS[attempt]
                            print(
                                f"Gemini TTS {quota_kind} quota HTTP 429 for {model}; retrying in {delay}s "
                                f"({attempt + 1}/{len(GEMINI_TTS_RETRY_DELAYS)})",
                                flush=True,
                            )
                            time.sleep(delay)
                            continue
                        exhausted_models.append(model)
                        if model_index + 1 < len(models):
                            print(
                                f"Gemini TTS rate limit persisted for {model}; "
                                f"switching to free fallback {models[model_index + 1]}",
                                flush=True,
                            )
                            break
                        raise RuntimeError(
                            "BLOCKED_COST_OR_FREE_LIMIT: Gemini TTS rate limit persisted for all "
                            f"configured models ({', '.join(exhausted_models)}); saved chunks will resume later"
                        ) from exc
                    if exc.code in (401, 403):
                        raise RuntimeError(f"Gemini TTS authentication failed with HTTP {exc.code}") from exc
                    raise RuntimeError(f"Gemini TTS model {model} failed with HTTP {exc.code}") from exc
            if result is not None:
                break
        if result is None:
            raise RuntimeError("Gemini TTS returned no result")
        audio = base64.b64decode(_gemini_audio_data(result), validate=True)
        if len(audio) <= 1024 or not audio.startswith(b"RIFF"):
            raise RuntimeError("Gemini TTS returned invalid or empty WAV audio")
        wav_path.write_bytes(audio)
        run([ffmpeg(), "-y", "-i", str(wav_path), "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
    finally:
        wav_path.unlink(missing_ok=True)

async def tts_one(text: str, out: Path):
    provider = SETTINGS.get("tts_provider", "edge").lower()
    if provider in ("fish", "munsit", "gemini"):
        last_error = None
        for attempt in range(3):
            try:
                worker = {
                    "fish": fish_tts_one,
                    "munsit": munsit_tts_one,
                    "gemini": gemini_tts_one,
                }[provider]
                await asyncio.to_thread(worker, text, out)
                return
            except RuntimeError as exc:
                if "BLOCKED_COST" in str(exc):
                    raise
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"{provider} transient request failed after 3 attempts: {type(last_error).__name__}") from last_error
    await edge_tts_one(text, out)

def voice_metadata() -> dict:
    provider = SETTINGS.get("tts_provider", "edge").lower()
    if provider == "gemini":
        return {
            "voice_provider": "gemini",
            "voice_name": SECRETS.get("GEMINI_TTS_VOICE_NAME", SETTINGS.get("gemini_tts_voice_name", "راوي القصص 1")),
            "voice_id": SECRETS.get("GEMINI_TTS_VOICE_ID", ""),
            "tts_model": SETTINGS.get("gemini_tts_model", "gemini-3.8-flash-tts"),
            "tts_generation_speed": float(SETTINGS.get("tts_generation_speed", 1.0)),
            "post_processing_tempo": float(SETTINGS.get("post_tts_tempo", 1.0)),
            "effective_speed": float(SETTINGS.get("post_tts_tempo", 1.0)),
            "diacritization_engine": DIACRITIZATION_VERSION,
            "sentence_pause_strategy": SETTINGS.get("sentence_pause_strategy", "punctuation_and_blank_line"),
        }
    if provider == "fish":
        return {
            "voice_provider": "fish",
            "voice_name": SECRETS.get("FISH_VOICE_NAME", SETTINGS.get("fish_voice_name", "ناشونال جيوغرافيك")),
            "voice_id": SECRETS.get("FISH_VOICE_ID", ""),
            "tts_model": SETTINGS.get("fish_model", "s2.1-pro-free"),
            "tts_generation_speed": float(SETTINGS.get("tts_generation_speed", 1.0)),
            "post_processing_tempo": float(SETTINGS.get("post_tts_tempo", 1.0)),
            "effective_speed": float(SETTINGS.get("post_tts_tempo", 1.0)),
            "diacritization_engine": DIACRITIZATION_VERSION,
            "sentence_pause_strategy": SETTINGS.get("sentence_pause_strategy", "punctuation_and_blank_line"),
        }
    if provider == "munsit":
        return {
            "voice_provider": "munsit",
            "voice_name": SECRETS.get("MUNSIT_VOICE_NAME", SETTINGS.get("munsit_voice_name", "جاد")),
            "voice_id": SECRETS.get("MUNSIT_VOICE_ID", ""),
            "tts_model": SETTINGS.get("munsit_model", "faseeh-v1-preview"),
            "tts_generation_speed": float(SETTINGS.get("tts_generation_speed", 1.0)),
            "post_processing_tempo": float(SETTINGS.get("post_tts_tempo", 1.0)),
            "effective_speed": float(SETTINGS.get("post_tts_tempo", 1.0)),
            "diacritization_engine": DIACRITIZATION_VERSION,
            "sentence_pause_strategy": SETTINGS.get("sentence_pause_strategy", "punctuation_and_blank_line"),
        }
    return {
        "voice_provider": "edge",
        "voice_name": SETTINGS["tts_voice"],
        "voice_id": "",
        "tts_model": "Microsoft Edge neural TTS free fallback",
        "tts_generation_speed": float(SETTINGS.get("tts_generation_speed", 1.0)),
        "post_processing_tempo": float(SETTINGS.get("post_tts_tempo", 1.0)),
        "effective_speed": float(SETTINGS.get("post_tts_tempo", 1.0)),
        "diacritization_engine": DIACRITIZATION_VERSION,
        "sentence_pause_strategy": SETTINGS.get("sentence_pause_strategy", "punctuation_and_blank_line"),
    }

def mix_background_audio(narration: Path, output: Path) -> None:
    configured = SETTINGS.get("background_audio_file", "")
    background = (ROOT / configured).resolve() if configured else None
    if not background or not background.exists() or background.stat().st_size <= 1024:
        raise RuntimeError(f"Background audio is missing or empty: {background}")
    volume = float(SETTINGS.get("background_audio_volume", 0.15))
    if not 0.0 <= volume <= 1.0:
        raise RuntimeError(f"Invalid background_audio_volume: {volume}")
    exact_duration = duration(narration)
    filter_graph = (
        f"[1:a]volume={volume:.4f}[background];"
        "[0:a][background]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[out]"
    )
    run([
        ffmpeg(), "-y", "-i", str(narration), "-stream_loop", "-1", "-i", str(background),
        "-filter_complex", filter_graph, "-map", "[out]", "-t", f"{exact_duration:.3f}",
        "-c:a", "libmp3lame", "-b:a", "160k", str(output),
    ])

def prepare(story: Path):
    clean_path=story/"05_story_clean.md"
    clean_story=clean_path.read_text(encoding="utf-8").strip()
    extension=story/"draft_extension.md"
    extension_marker="<!-- MERGED_DRAFT_EXTENSION -->"
    if extension.exists() and extension_marker not in clean_story and arabic_words(clean_story) < 5000:
        clean_story += f"\n\n{extension_marker}\n\n" + extension.read_text(encoding="utf-8").strip()
        clean_path.write_text(clean_story+"\n",encoding="utf-8")
    logs = story / "logs"
    logs.mkdir(exist_ok=True)
    source_hash = hashlib.sha256(clean_story.encode("utf-8")).hexdigest()
    prepare_manifest_path = logs / "tts_prepare.json"
    prepare_manifest = {}
    if prepare_manifest_path.exists():
        prepare_manifest = json.loads(prepare_manifest_path.read_text(encoding="utf-8"))
    annotated_path=story/"06_story_tts_annotated.md"
    reusable = (
        prepare_manifest.get("source_sha256") == source_hash
        and prepare_manifest.get("version") == DIACRITIZATION_VERSION
        and annotated_path.exists()
        and diacritic_ratio(annotated_path.read_text(encoding="utf-8")) >= 0.27
    )
    if reusable:
        annotated = annotated_path.read_text(encoding="utf-8").strip()
    else:
        annotated = diacritize_and_annotate(clean_story, logs / "diacritics_cache")
        annotated_path.write_text(annotated+"\n",encoding="utf-8")
        prepare_manifest_path.write_text(json.dumps({
            "source_sha256": source_hash,
            "version": DIACRITIZATION_VERSION,
            "diacritic_ratio": round(diacritic_ratio(annotated), 4),
            "direction_tags_human_only": True,
            "direction_tags_sent_to_fish": False,
            "review_passes": int(SETTINGS.get("diacritization_review_passes", 2)),
            "tts_generation_speed": float(SETTINGS.get("tts_generation_speed", 1.0)),
            "post_processing_tempo": float(SETTINGS.get("post_tts_tempo", 1.0)),
        }, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    clean = api_text(annotated)
    (story / "07_story_tts_api.md").write_text(clean + "\n", encoding="utf-8")
    provider = SETTINGS.get("tts_provider", "edge").lower()
    chunk_limit = int(
        SETTINGS.get("gemini_tts_chunk_char_limit", 4200)
        if provider == "gemini"
        else SETTINGS.get("tts_chunk_char_limit", 2600)
    )
    chunks = split_chunks(clean, limit=chunk_limit)
    tdir = story / "tts_chunks"; tdir.mkdir(exist_ok=True)
    for old_chunk in tdir.glob("*.txt"):
        old_chunk.unlink()
    for i, chunk in enumerate(chunks, 1):
        (tdir / f"{i:03}.txt").write_text(chunk + "\n", encoding="utf-8")
    print(f"Prepared {len(chunks)} chunks, {arabic_words(clean)} Arabic words, diacritics={diacritic_ratio(annotated):.3f}")

async def synthesize(story: Path):
    provider = SETTINGS.get("tts_provider", "edge").lower()
    if provider == "fish":
        # Fail before generating any paid/free-tier chunks if the credential
        # points at a different voice than the selected model.
        await asyncio.to_thread(validate_fish_voice)
    elif provider == "munsit":
        await asyncio.to_thread(validate_munsit_voice)
    elif provider == "gemini":
        await asyncio.to_thread(validate_gemini_voice)
    prepare(story)
    tdir, adir = story / "tts_chunks", story / "audio_chunks"
    adir.mkdir(exist_ok=True)
    hash_path = story / "logs" / "tts_chunk_hashes.json"
    old_hashes = json.loads(hash_path.read_text(encoding="utf-8")) if hash_path.exists() else {}
    new_hashes = {}
    for src in sorted(tdir.glob("*.txt")):
        out = adir / (src.stem + ".mp3")
        text = src.read_text(encoding="utf-8")
        provider_voice_id = (
            SECRETS.get("FISH_VOICE_ID", "") if provider == "fish"
            else SECRETS.get("MUNSIT_VOICE_ID", "") if provider == "munsit"
            else SECRETS.get("GEMINI_TTS_VOICE_ID", "") if provider == "gemini"
            else SETTINGS.get("tts_voice", "")
        )
        provider_model = (
            SETTINGS.get("fish_model", "s2.1-pro-free") if provider == "fish"
            else SETTINGS.get("munsit_model", "faseeh-v1-preview") if provider == "munsit"
            else SETTINGS.get("gemini_tts_model", "gemini-3.8-flash-tts") if provider == "gemini"
            else "edge"
        )
        source_hash = hashlib.sha256(
            (
                TTS_AUDIO_CACHE_VERSION
                + "\n"
                + provider
                + "\n"
                + provider_model
                + "\n"
                + provider_voice_id
                + "\n"
                + text
            ).encode("utf-8")
        ).hexdigest()
        new_hashes[src.name] = source_hash
        if out.exists() and out.stat().st_size > 1024 and old_hashes.get(src.name) == source_hash:
            hash_path.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            continue
        await tts_one(text, out)
        if out.stat().st_size <= 1024: raise RuntimeError(f"empty audio chunk: {out}")
        # Persist progress after every successful chunk so an interrupted run
        # resumes from the next missing/stale chunk instead of starting over.
        hash_path.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    valid_stems = {path.stem for path in tdir.glob("*.txt")}
    for stale_audio in adir.glob("*.mp3"):
        if stale_audio.stem not in valid_stems:
            stale_audio.unlink()
    hash_path.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    concat = adir / "concat.txt"
    concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in sorted(adir.glob("*.mp3"))), encoding="utf-8")
    tempo = float(SETTINGS.get("post_tts_tempo", 1.0))
    if tempo != 1.0:
        raise RuntimeError(f"Post-TTS tempo guard failed: expected native 1.0, got {tempo}")
    narration = story / "12_audio_narration.mp3"
    run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c:a", "libmp3lame", "-b:a", "160k", str(narration)])
    full = story / "12_audio_full.mp3"
    mix_background_audio(narration, full)
    review = story / "13_audio_review.opus"
    run([ffmpeg(), "-y", "-i", str(full), "-c:a", "libopus", "-b:a", "48k", str(review)])
    print("Audio duration", duration(full))

def background(story: Path, force: bool = True):
    output = story / "10_background.png"
    thumbnail = story / "11_thumbnail_base.jpg"
    if not force and output.exists() and thumbnail.exists() and output.stat().st_size > 250_000:
        return
    generator = ROOT / "scripts" / "image_generate.py"
    run([sys.executable, str(generator), str(story)])

def video_codec() -> list[str]:
    # GitHub-hosted runners expose NVENC in FFmpeg's encoder list but do not
    # provide an NVIDIA GPU. Force the CPU encoder there and retain NVENC on
    # local machines where it is available.
    if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
        return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]
    encoders = subprocess.run(
        [ffmpeg(), "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
    ).stdout
    if "h264_nvenc" in encoders:
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "30", "-b:v", "0"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "28"]

def render(story: Path):
    background(story, force=False)
    audio=story/"12_audio_full.mp3"; out=story/"14_video_final.mp4"
    exact_duration=duration(audio)
    codec=video_codec()
    run([ffmpeg(),"-y","-loop","1","-framerate","1","-i",str(story/"10_background.png"),"-i",str(audio),"-vf","format=yuv420p","-r","2",*codec,"-c:a","aac","-b:a","128k","-t",f"{exact_duration:.3f}","-movflags","+faststart",str(out)])

def validate(story: Path):
    required=["01_title.txt","02_description.txt","03_sources.md","04_research_notes.md","05_story_clean.md","06_story_tts_annotated.md","07_story_tts_api.md","10_background.png","11_thumbnail_base.jpg","12_audio_narration.mp3","12_audio_full.mp3","13_audio_review.opus","14_video_final.mp4","15_metadata.json","16_fact_check.md","17_status.md"]
    missing=[x for x in required if not (story/x).exists()]
    if missing: raise RuntimeError("Missing: "+", ".join(missing))
    annotated_text=(story/"06_story_tts_annotated.md").read_text(encoding="utf-8")
    api_tts_text=(story/"07_story_tts_api.md").read_text(encoding="utf-8")
    clean_story_text=(story/"05_story_clean.md").read_text(encoding="utf-8")
    faithful, lexical_corrections=lexical_fidelity(clean_story_text, annotated_text)
    if not faithful:
        raise RuntimeError("TTS annotated text differs lexically from the clean story")
    current_diacritic_ratio=diacritic_ratio(annotated_text)
    if current_diacritic_ratio < 0.27:
        raise RuntimeError(f"Arabic TTS text is not fully diacritized: ratio={current_diacritic_ratio:.3f}")
    human_tags=DIRECTION_RE.findall(annotated_text)
    fish_tags=DIRECTION_RE.findall(api_tts_text)
    if not human_tags:
        raise RuntimeError("Human review copy is missing restrained performance cues")
    if fish_tags:
        raise RuntimeError(f"Direction tags must not be sent to Fish: {fish_tags[:3]}")
    quality_errors=diacritization_quality_errors(annotated_text)
    if quality_errors:
        raise RuntimeError(f"Arabic diacritization quality guard failed: {quality_errors}")
    if float(SETTINGS.get("tts_generation_speed", 0)) != 1.0:
        raise RuntimeError("TTS must generate at its normal speed 1.0")
    if float(SETTINGS.get("post_tts_tempo", 0)) != 1.0:
        raise RuntimeError("Post-processing tempo must remain at native speed 1.0")
    if re.search(r"ة[\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]+(?=\s*[.!؟…؛]|\s*$)", api_tts_text):
        raise RuntimeError("API TTS text contains a vocalized taa marbuta at a pausal boundary")
    sentence_boundaries=len(re.findall(r"[.!؟]\n\n", api_tts_text))
    if sentence_boundaries < 5:
        raise RuntimeError("TTS text does not contain enough sentence pause boundaries")
    ad,vd=duration(story/"12_audio_full.mp3"),duration(story/"14_video_final.mp4")
    if abs(ad-vd)>2.5: raise RuntimeError(f"duration mismatch {ad} {vd}")
    logs=story/"logs"; logs.mkdir(exist_ok=True)
    silence=subprocess.run([ffmpeg(),"-hide_banner","-i",str(story/"12_audio_full.mp3"),"-af","silencedetect=noise=-45dB:d=8","-f","null","-"],capture_output=True,text=True)
    long_silences=re.findall(r"silence_duration:\s*([0-9.]+)",silence.stderr)
    stats=subprocess.run([ffmpeg(),"-hide_banner","-i",str(story/"12_audio_full.mp3"),"-af","astats=metadata=1:reset=1","-f","null","-"],capture_output=True,text=True)
    duration_status="PASS" if SETTINGS["target_min_seconds"] <= ad <= SETTINGS["target_max_seconds"] else "REVIEW"
    validation={"audio_duration_seconds":round(ad,2),"video_duration_seconds":round(vd,2),"duration_status":duration_status,"tts_diacritization_status":"PASS_DOUBLE_REVIEWED_PAUSAL","diacritic_ratio":round(current_diacritic_ratio,4),"diacritization_engine":DIACRITIZATION_VERSION,"tts_generation_speed":float(SETTINGS.get("tts_generation_speed",1.0)),"post_processing_tempo":float(SETTINGS.get("post_tts_tempo",1.0)),"effective_speed":float(SETTINGS.get("post_tts_tempo",1.0)),"sentence_pause_strategy":SETTINGS.get("sentence_pause_strategy"),"sentence_pause_boundaries":sentence_boundaries,"pausal_taa_marbuta_status":"PASS","lexical_corrections_count":len(lexical_corrections),"lexical_corrections_preview":lexical_corrections[:20],"human_direction_tags":len(human_tags),"fish_direction_tags_sent":len(fish_tags),"long_silences_seconds":[float(x) for x in long_silences],"audio_chunks":len(list((story/"audio_chunks").glob("*.mp3"))),"tts_chunks":len(list((story/"tts_chunks").glob("*.txt"))),"zero_byte_files":[str(p) for p in story.rglob("*") if p.is_file() and p.stat().st_size==0]}
    (logs/"validation.json").write_text(json.dumps(validation,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (logs/"ffmpeg_astats.txt").write_text(stats.stderr,encoding="utf-8")
    if validation["zero_byte_files"]: raise RuntimeError("zero-byte files found")
    if validation["long_silences_seconds"]: raise RuntimeError("unnatural silence >= 8 seconds detected")
    image_source=(story/"09_image_source.md").read_text(encoding="utf-8") if (story/"09_image_source.md").exists() else f"{SETTINGS.get('image_provider')} {SETTINGS.get('image_model')}"; meta=json.loads((story/"15_metadata.json").read_text(encoding="utf-8")); meta.update(audio_duration_seconds=round(ad,2),video_duration_seconds=round(vd,2),word_count=arabic_words((story/"05_story_clean.md").read_text(encoding="utf-8")),**voice_metadata(),fact_check_status="completed",image_method=image_source,background_audio="pink noise",background_audio_volume_percent=15,narration_audio_path=str(story/"12_audio_narration.mp3"),audio_path=str(story/"12_audio_full.mp3"),video_path=str(story/"14_video_final.mp4"),thumbnail_path=str(story/"11_thumbnail_base.jpg"),approval_status="pending_approval",publish_status="not_published")
    (story/"15_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (story/"17_status.md").write_text(
        "# الحالة\n\n- الحالة الحالية: `pending_approval`\n- النشر: `not_published`\n- بانتظار مراجعة Marwa واعتماد صريح قبل النشر.\n",
        encoding="utf-8",
    )
    history_path=ROOT/"config"/"topics_history.json"
    history=json.loads(history_path.read_text(encoding="utf-8"))
    for topic in history:
        if topic.get("slug") == meta.get("slug"):
            topic["status"]="pending_approval"
            topic["story_path"]=story.relative_to(ROOT).as_posix()
    history_path.write_text(json.dumps(history,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"metadata":meta,"validation":validation},ensure_ascii=False,indent=2))

def package(story: Path):
    meta=json.loads((story/"15_metadata.json").read_text(encoding="utf-8"))
    image_source=(story/"09_image_source.md").read_text(encoding="utf-8") if (story/"09_image_source.md").exists() else f"{SETTINGS.get('image_provider')} {SETTINGS.get('image_model')}"; meta.update(audio_duration_seconds=round(duration(story/"12_audio_full.mp3"),2),video_duration_seconds=round(duration(story/"14_video_final.mp4"),2),word_count=arabic_words((story/"05_story_clean.md").read_text(encoding="utf-8")),**voice_metadata(),image_method=image_source,background_audio="pink noise",background_audio_volume_percent=15,narration_audio_path=str(story/"12_audio_narration.mp3"),audio_path=str(story/"12_audio_full.mp3"),video_path=str(story/"14_video_final.mp4"),thumbnail_path=str(story/"11_thumbnail_base.jpg"),approval_status="pending_approval",publish_status="not_published")
    (story/"15_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (story/"17_status.md").write_text(
        "# الحالة\n\n- الحالة الحالية: `pending_approval`\n- النشر: `not_published`\n- بانتظار مراجعة Marwa واعتماد صريح قبل النشر.\n",
        encoding="utf-8",
    )
    def md(name): return html.escape((story/name).read_text(encoding="utf-8"))
    page=f'''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>{html.escape(meta['title'])}</title><style>body{{font-family:Segoe UI,Tahoma,sans-serif;background:#f4efe5;color:#4f4035;max-width:1050px;margin:auto;padding:32px;line-height:1.9}}img{{width:100%;border-radius:18px}}.card{{background:#fffaf0;padding:24px;margin:18px 0;border:1px solid #cec7b6;border-radius:16px}}pre{{white-space:pre-wrap;font:inherit}}a{{color:#826d5c}}</style><h1>{html.escape(meta['title'])}</h1><img src="10_background.png"><div class="card"><b>الموضوع:</b> {html.escape(meta['topic'])}<br><b>الكلمات:</b> {meta.get('word_count',0)}<br><b>مدة الصوت:</b> {meta.get('audio_duration_seconds',0)} ثانية<br><b>الخلفية الصوتية:</b> Pink Noise بنسبة 15%<br><b>الحالة:</b> pending_approval<br><a href="12_audio_narration.mp3">صوت الراوي فقط</a> · <a href="12_audio_full.mp3">الصوت المخلوط</a> · <a href="13_audio_review.opus">نسخة المراجعة</a> · <a href="14_video_final.mp4">الفيديو</a> · <a href="11_thumbnail_base.jpg">Thumbnail</a></div><h2>النص</h2><div class="card"><pre>{md('05_story_clean.md')}</pre></div><h2>وصف YouTube</h2><div class="card"><pre>{md('02_description.txt')}</pre></div><h2>المصادر</h2><div class="card"><pre>{md('03_sources.md')}</pre></div></html>'''
    (story/"00_package.html").write_text(page,encoding="utf-8")

def main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["prepare","tts","image","render","validate","package","all"]); p.add_argument("story")
    a=p.parse_args(); story=Path(a.story).resolve()
    if a.command in ("prepare","all"): prepare(story)
    if a.command in ("tts","all"): asyncio.run(synthesize(story))
    if a.command in ("image","all"): background(story)
    if a.command in ("render","all"): render(story)
    if a.command in ("validate","all"): validate(story)
    if a.command in ("package","all"): package(story)

if __name__ == "__main__": main()

