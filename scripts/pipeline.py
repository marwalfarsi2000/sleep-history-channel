from __future__ import annotations

import argparse, asyncio, base64, hashlib, html, json, os, re, shutil, subprocess, sys, time
import datetime as dt
import urllib.error
import urllib.request
import difflib
from pathlib import Path
from PIL import Image
import edge_tts
from generate_daily import gemini, parse_json
import run_summary
from stage_manifest import record_stage, can_reuse_stage, compute_config_hash, compute_file_hash

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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()

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
DIACRITIZATION_VERSION = "full-v10-pronunciation-accurate-pausal"
TTS_NORMALIZATION_VERSION = "arabic-speech-normalizer-v3-spoken-form"
TTS_AUDIO_CACHE_VERSION = "native-speed-1.0-v5-pronunciation-accurate"


class AudioTranscriptMismatch(RuntimeError):
    """One TTS chunk is decodable but does not faithfully match its source text."""

    def __init__(self, chunk_name: str, similarity: float, length_ratio: float):
        self.chunk_name = chunk_name
        self.chunk_stem = Path(chunk_name).stem
        self.similarity = similarity
        self.length_ratio = length_ratio
        super().__init__(
            f"AUDIO_QC_FAILED: transcript mismatch in {chunk_name} "
            f"(similarity={similarity:.3f}, length_ratio={length_ratio:.3f})"
        )
TTS_RULES_PATH = ROOT / "config" / "tts_speech_normalization_rules.md"
PRONUNCIATION_DICTIONARY_PATH = ROOT / "config" / "pronunciation_dictionary.json"
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

def align_source_diacritics(source: str, candidate: str) -> str:
    """Rebuild `source` with the candidate's marks wherever the words align.

    Used when the model merged, split, dropped, or changed words: aligned words
    keep their vocalization on the original letters, unaligned words stay as
    written in the source, so the result can never change the story text.
    """
    word_pattern = re.compile(r"(?:[ء-ي][ؐ-ًؚ-ٰٟۖ-ۭ]*)+")
    source_matches = list(word_pattern.finditer(source))
    candidate_words = [match.group() for match in word_pattern.finditer(without_direction_tags(candidate))]
    source_bare = [DIACRITICS_RE.sub("", match.group()) for match in source_matches]
    candidate_bare = [DIACRITICS_RE.sub("", word) for word in candidate_words]
    chosen = [match.group() for match in source_matches]
    matcher = difflib.SequenceMatcher(None, source_bare, candidate_bare, autojunk=False)
    for opcode, i1, i2, j1, j2 in matcher.get_opcodes():
        if opcode == "equal":
            for offset in range(i2 - i1):
                chosen[i1 + offset] = candidate_words[j1 + offset]
        elif opcode == "replace" and (i2 - i1) == (j2 - j1):
            for offset in range(i2 - i1):
                restored = restore_source_lexemes(source_bare[i1 + offset], candidate_words[j1 + offset])
                if DIACRITICS_RE.sub("", restored) == source_bare[i1 + offset]:
                    chosen[i1 + offset] = restored
    pieces, cursor = [], 0
    for match, word in zip(source_matches, chosen):
        pieces.append(source[cursor:match.start()])
        pieces.append(word)
        cursor = match.end()
    pieces.append(source[cursor:])
    return "".join(pieces)


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


def speech_safe_violations(text: str) -> list[str]:
    """Return deterministic hazards that must never reach a speech provider."""
    # Combining Arabic marks are not token boundaries. Scan an unvocalized
    # mirror so a word such as مِنهُ is never mistaken for standalone "م".
    scan_text = DIACRITICS_RE.sub("", text)
    checks = (
        (r"[0-9٠-٩]", "raw number"),
        (r"https?://|www\.", "URL"),
        (r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "email"),
        (r"@[A-Za-z0-9_]", "username"),
        (r"[A-Za-z]", "Latin text or abbreviation"),
        (r"<[^>]+>", "HTML"),
        (r"(?m)^\s{0,3}(?:#{1,6}|[-+*]>?|>)\s+|```|`", "Markdown"),
        (r"\[\s*\d+\s*\]|\(\s*[^)]*\d{4}[^)]*\)", "citation or footnote"),
        (r"[%٪°$€£¥+×÷=&<>/@#*_\\²³]", "unexpanded symbol"),
        (r"[–—]", "unresolved dash"),
        (r"(?<!\w)-(?!\w)|(?<=\w)-(?=\w)", "unresolved hyphen"),
        (r"[()\[\]{}]", "parenthetical or bracketed text"),
        (r"[\"“”«»]", "quotation marks requiring spoken context"),
        (r"(?:\.{3,}|[!؟?]{2,})", "repeated punctuation"),
        (r"(?<![\w])(?:هـ|ق\.?م\.?|سم|كغ|د\.|أ\.?د\.?|ص|م)(?![\w])", "ambiguous abbreviation"),
        # "كم" is also an ordinary word ("how many"); it is only the kilometre abbreviation right after a number word.
        (r"(?<![\w])(?:واحد|اثنين|اثنان|ثلاث\w*|أربع\w*|خمس\w*|ست\w*|سبع\w*|ثمان\w*|تسع\w*|عشر\w*|مئة|مائة|مئتين|مئتان|ألف|ألفين|مليون)\s+كم(?![\w])", "ambiguous abbreviation"),
    )
    violations: list[str] = []
    for pattern, label in checks:
        match = re.search(pattern, scan_text, flags=re.IGNORECASE)
        if match:
            sample = " ".join(match.group(0).split())[:60]
            violations.append(f"{label}: {sample}")
    return violations


def require_speech_safe(text: str, *, stage: str) -> None:
    violations = speech_safe_violations(text)
    if violations:
        raise RuntimeError(
            f"SPEECH_SAFE_VALIDATION_FAILED[{stage}]: "
            + json.dumps(violations, ensure_ascii=False)
        )


def _strip_model_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        value = re.sub(r"^```[^\n]*\n?", "", value)
        value = re.sub(r"\n?```$", "", value)
    return value.strip()


def validate_speech_safe_semantics(source: str, candidate: str) -> dict:
    prompt = f"""أنت SPEECH_SAFE_VALIDATOR مستقل. لا تعيد كتابة النص.
قارن النص الأصلي بنسخة النطق، وتأكد أن كل حقيقة ومعنى وعلاقة زمنية وكمية محفوظة، وأن الأرقام والتواريخ والعصور والوحدات والاختصارات فُهمت من السياق ولم تُخمن، وأن الصياغة عربية طبيعية لما يجب أن يسمعه المستمع.
أعد JSON فقط:
{{"status":"PASS أو FAIL","meaning_preserved":true,"context_resolved":true,"natural_spoken_arabic":true,"remaining_ambiguities":[],"issues":[]}}

النص الأصلي:
{source}

نسخة النطق:
{candidate}
"""
    result_text, _ = gemini(prompt, json_mode=True, max_tokens=1800, temperature=0.0)
    result = parse_json(result_text)
    passed = (
        result.get("status") == "PASS"
        and result.get("meaning_preserved") is True
        and result.get("context_resolved") is True
        and result.get("natural_spoken_arabic") is True
        and not (result.get("remaining_ambiguities") or [])
        and not (result.get("issues") or [])
    )
    if not passed:
        result["status"] = "FAIL"
    return result


def normalize_for_tts(clean_story: str, story: Path) -> str:
    """Create and checkpoint an ear-first narration without changing facts."""
    rules = TTS_RULES_PATH.read_text(encoding="utf-8")
    cache_dir = story / "logs" / "tts_normalizer_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []
    outputs: list[str] = []
    blocks = split_diacritization_blocks(clean_story, limit=7000)
    for index, block in enumerate(blocks, 1):
        hazards = speech_safe_violations(block)
        if not hazards:
            outputs.append(block)
            records.append({"block": index, "status": "already_safe", "violations": []})
            continue
        cache_key = hashlib.sha256(
            f"{TTS_NORMALIZATION_VERSION}\n{rules}\n{block}".encode("utf-8")
        ).hexdigest()
        cache_path = cache_dir / f"{cache_key}.txt"
        cache_hit = cache_path.exists()
        candidate = cache_path.read_text(encoding="utf-8").strip() if cache_hit else ""
        last_violations = speech_safe_violations(candidate) if candidate else hazards
        semantic_gate: dict = {}
        if candidate and not last_violations:
            # A cache entry is written only after both deterministic and semantic
            # validators pass for this exact source/rules/version hash.
            semantic_gate = (
                {"status": "PASS", "cached": True}
                if cache_hit
                else validate_speech_safe_semantics(block, candidate)
            )
        if not candidate or last_violations or semantic_gate.get("status") != "PASS":
            for _attempt in range(2):
                prompt = f"""أنت TTS_NORMALIZER عربي مستقل. حوّل النص المكتوب إلى نص نطق طبيعي فقط.
لا تلخص ولا تحذف أي حقيقة سردية ولا تضف معلومة. غيّر فقط ما يلزم للنطق: الأرقام والتواريخ والعصور والاختصارات والوحدات والعملات والرموز والأسماء الأجنبية والتنسيق.
استخدم السياق داخل المقطع لاختيار الصيغة العربية الطبيعية. عند الغموض أعد صياغة الجملة بلا تخمين.
أخرج النص المنظَّم وحده دون عنوان أو Markdown أو شرح.

القواعد الإلزامية:
{rules}

المخاطر أو ملاحظات المدقق المستقل:
{json.dumps(last_violations or semantic_gate, ensure_ascii=False)}

النص:
{block}
"""
                result, _ = gemini(prompt, max_tokens=8000, temperature=0.0)
                candidate = _strip_model_fence(result)
                last_violations = speech_safe_violations(candidate)
                minimum_words = max(1, int(arabic_words(block) * 0.85))
                semantic_gate = (
                    validate_speech_safe_semantics(block, candidate)
                    if not last_violations and arabic_words(candidate) >= minimum_words
                    else {"status": "FAIL", "issues": ["output became too short"]}
                )
                if not last_violations and semantic_gate.get("status") == "PASS":
                    cache_path.write_text(candidate + "\n", encoding="utf-8")
                    break
            else:
                raise RuntimeError(
                    f"TTS_NORMALIZATION_FAILED block {index}: "
                    + json.dumps(last_violations or semantic_gate, ensure_ascii=False)
                )
        outputs.append(candidate)
        records.append({
            "block": index,
            "status": "normalized",
            "violations": hazards,
            "semantic_validation": semantic_gate,
        })
    normalized = "\n\n".join(outputs).strip()
    require_speech_safe(normalized, stage="TTS_NORMALIZER_OUTPUT")
    (story / "logs" / "tts_normalization.json").write_text(
        json.dumps({
            "version": TTS_NORMALIZATION_VERSION,
            "status": "passed",
            "blocks": records,
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return normalized

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


SENTENCE_FINAL_WORD_RE = re.compile(
    r"((?:[\u0621-\u064A][\u0610-\u061A\u064B-\u065F\u0670\u06D6-\u06ED]*)+)"
    r"(?=\s*(?:[.!؟…؛]+|$))"
)
FINAL_CASE_ENDING_RE = re.compile(r"[\u064B-\u0650]")


def _strip_final_case_ending(word: str) -> str:
    """Preserve the word's pronunciation marks while removing only the spoken case ending at waqf."""
    graphemes: list[str] = []
    for char in word:
        if DIACRITICS_RE.fullmatch(char) and graphemes:
            graphemes[-1] += char
        else:
            graphemes.append(char)
    if not graphemes:
        return word
    last = graphemes[-1]
    base = DIACRITICS_RE.sub("", last)
    marks = "".join(DIACRITICS_RE.findall(last))
    # At a sentence stop, Arabic drops the final short grammatical vowel/tanween.
    # Keep lexical/internal marks, shadda, sukun and dagger alif so the word itself
    # remains fully guided for pronunciation.
    marks = FINAL_CASE_ENDING_RE.sub("", marks)
    graphemes[-1] = base + marks

    # Tanween al-fath is commonly encoded on the consonant before a support alif,
    # e.g. واضِحًا / طَوِيلًا.  At waqf the tanween disappears while the support
    # alif remains, giving the actually pronounced form واضِحا / طَوِيلا.
    if len(graphemes) >= 2 and base in {"ا", "ى"}:
        previous = graphemes[-2]
        previous_base = DIACRITICS_RE.sub("", previous)
        previous_marks = "".join(DIACRITICS_RE.findall(previous)).replace("\u064b", "")
        graphemes[-2] = previous_base + previous_marks

    return "".join(graphemes)


def apply_pausal_sentence_endings(text: str) -> str:
    """Apply natural Arabic waqf without stripping internal pronunciation guidance."""
    return SENTENCE_FINAL_WORD_RE.sub(
        lambda match: _strip_final_case_ending(match.group(1)),
        text,
    )


def vocalized_sentence_final_words(text: str) -> list[str]:
    violations: list[str] = []
    for match in SENTENCE_FINAL_WORD_RE.finditer(text):
        word = match.group(1)
        graphemes: list[str] = []
        for char in word:
            if DIACRITICS_RE.fullmatch(char) and graphemes:
                graphemes[-1] += char
            else:
                graphemes.append(char)
        if graphemes and FINAL_CASE_ENDING_RE.search(graphemes[-1]):
            violations.append(word)
    return violations


def require_pausal_sentence_endings(text: str) -> None:
    vocalized_endings = vocalized_sentence_final_words(text)
    if vocalized_endings:
        raise RuntimeError(
            "TTS_PAUSAL_ENDINGS_FAILED: vocalized sentence-final words: "
            + json.dumps(vocalized_endings[:5], ensure_ascii=False)
        )


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
            outputs.append(candidate)
            continue
        prompt = f"""أنت مدقق لغوي عربي متخصص في النصوص المهيأة للتحويل إلى صوت عربي فصيح.

المطلوب:
1. أعد النص نفسه حرفيًا دون حذف أو إضافة أو استبدال أي كلمة أو رقم أو علامة ترقيم.
2. ضع التشكيل العربي الكامل الصحيح في نسخة المراجعة، بما في ذلك الكلمات الأخيرة: الفتحة والضمة والكسرة والسكون والشدة والتنوين حيث يلزم. ستُحذف علامات تشكيل آخر كلمة في كل جملة آليًا لاحقًا من نسخة TTS فقط لتطبيق الوقف.
3. راعِ السياق النحوي والدلالي، وثبّت نطق أسماء الأشخاص والمدن والمصطلحات التاريخية بصورة متسقة.
4. راجع النطق الداخلي للكلمة الأخيرة بدقة، ولا تحذف حروفًا أو تغيّر هجاءها؛ تطبيق الوقف وحذف تشكيلها من نسخة TTS يتمان آليًا بعد المراجعة.
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
                # Keep the marks that align and restore every original word.
                candidate = normalize_arabic_diacritics(align_source_diacritics(block, candidate))
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
- راجع الكلمات الأخيرة مشكولة بالكامل هنا لتثبيت نطقها، ولا تغيّر حروفها؛ ستزال جميع علامات تشكيل آخر كلمة في كل جملة آليًا من نسخة TTS لتطبيق الوقف الطبيعي.
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
            outputs.append(candidate)
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
    # Never send bracketed production cues to the speech provider.
    text = without_direction_tags(text)
    # Preserve pronunciation-accurate vocalization, but convert sentence-final
    # words to natural Arabic pausal form so grammatical endings/tanween are not
    # pronounced at a stop.
    text = apply_pausal_sentence_endings(text)
    # Speech providers respect punctuation and paragraph boundaries. A blank line
    # after a completed sentence creates a short natural pause without SSML.
    # A dot between digits is a decimal separator, not a sentence boundary.
    text = re.sub(r"\s*((?<!\d)\.|\.(?!\d)|[!؟])\s*", r"\1\n\n", text)
    text = re.sub(r"\s*(؛)\s*", r"\1\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def apply_pronunciation_replacement(text: str, current: str, replacement: str) -> tuple[str, int]:
    """Apply a mark-only correction even when Gemini omits existing marks.

    The pronunciation reviewer occasionally returns the right source letters
    with fewer (or differently ordered) diacritics than the annotated story.
    Exact matching would reject that safe correction.  Match a second time on
    the immutable, undiacritized character stream while preserving the
    original span boundaries.
    """
    current_bare = DIACRITICS_RE.sub("", current)
    replacement_bare = DIACRITICS_RE.sub("", replacement)
    if not current_bare or current_bare != replacement_bare:
        raise RuntimeError("PRONUNCIATION_VALIDATION_FAILED: pronunciation changes altered letters")

    if current in text:
        count = text.count(current)
        return text.replace(current, replacement), count

    bare_chars: list[str] = []
    source_indexes: list[int] = []
    for index, char in enumerate(text):
        if DIACRITICS_RE.fullmatch(char):
            continue
        bare_chars.append(char)
        source_indexes.append(index)
    bare_text = "".join(bare_chars)

    starts: list[int] = []
    cursor = 0
    while True:
        found = bare_text.find(current_bare, cursor)
        if found < 0:
            break
        starts.append(found)
        cursor = found + len(current_bare)
    if not starts:
        raise RuntimeError("PRONUNCIATION_VALIDATION_FAILED: proposed source text was not found")

    spans: list[tuple[int, int]] = []
    for start in starts:
        original_start = source_indexes[start]
        original_end = source_indexes[start + len(current_bare) - 1] + 1
        while original_end < len(text) and DIACRITICS_RE.fullmatch(text[original_end]):
            original_end += 1
        spans.append((original_start, original_end))
    for start, end in reversed(spans):
        text = text[:start] + replacement + text[end:]
    return text, len(spans)


def pronunciation_validate(clean_story: str, annotated: str, story: Path) -> str:
    dictionary = {}
    if PRONUNCIATION_DICTIONARY_PATH.exists():
        dictionary = json.loads(PRONUNCIATION_DICTIONARY_PATH.read_text(encoding="utf-8"))
    revised = annotated
    dictionary_applied = []
    bare_revised = DIACRITICS_RE.sub("", revised)
    for current, replacement in dictionary.items():
        current = str(current).strip()
        replacement = str(replacement).strip()
        if not current or not replacement or current not in bare_revised:
            continue
        revised, occurrences = apply_pronunciation_replacement(revised, current, replacement)
        dictionary_applied.append({
            "current": current,
            "replacement": replacement,
            "occurrences": occurrences,
        })
    prompt = f"""أنت بوابة نطق مستقلة لنص تاريخي عربي سيحوّل إلى صوت.
راجع فقط أسماء الأشخاص والمدن والمواقع والسلالات والحضارات والكلمات الأجنبية المعربة.
لا تغير الحقائق أو الكلمات. إذا كان التشكيل الحالي يسبب نطقًا خاطئًا، اقترح استبدالًا مشكولًا لنفس الحروف.
طُبق قاموس النطق الدائم أولًا. لا تتراجع عن تصحيحاته، واقترح فقط تصحيحات جديدة لازمة.
هذه نسخة المراجعة المشكولة بالكامل. ستُحذف آليًا جميع علامات تشكيل آخر كلمة في كل جملة من نسخة TTS النهائية لتطبيق الوقف، فلا تقترح تغيير الحروف لهذا السبب.
أعد JSON فقط:
{{"status":"PASS أو FAIL","replacements":[{{"current":"النص المشكول المطابق حرفيًا","replacement":"نفس الحروف بتشكيل أصح","reason":"..."}}],"unresolved":[]}}

النص المشكول:
{revised[:100000]}
"""
    result_text, _ = gemini(prompt, json_mode=True, max_tokens=5000, temperature=0.0)
    result = parse_json(result_text)
    validation_log = story / "logs" / "pronunciation_validation.json"
    validation_log.write_text(
        json.dumps({**result, "application_status": "validating"}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if result.get("status") not in {"PASS", "FAIL"}:
        raise RuntimeError("PRONUNCIATION_VALIDATION_FAILED: invalid response status")
    applied = []
    for item in result.get("replacements") or []:
        current = str(item.get("current", ""))
        replacement = str(item.get("replacement", ""))
        if not current or not replacement:
            raise RuntimeError("PRONUNCIATION_VALIDATION_FAILED: proposed source text was not found")
        revised, occurrences = apply_pronunciation_replacement(revised, current, replacement)
        applied.append({**item, "occurrences": occurrences})
        dictionary[DIACRITICS_RE.sub("", current)] = replacement
    faithful, _ = lexical_fidelity(clean_story, revised)
    if not faithful:
        raise RuntimeError("PRONUNCIATION_VALIDATION_FAILED: pronunciation changes altered letters")
    unresolved = result.get("unresolved") or []
    if result.get("status") != "PASS" or unresolved:
        raise RuntimeError("PRONUNCIATION_VALIDATION_FAILED: " + json.dumps(result, ensure_ascii=False))
    if applied:
        temporary_dictionary = PRONUNCIATION_DICTIONARY_PATH.with_suffix(".json.tmp")
        temporary_dictionary.write_text(
            json.dumps(dictionary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary_dictionary.replace(PRONUNCIATION_DICTIONARY_PATH)
    result["dictionary_applied"] = dictionary_applied
    result["applied"] = applied
    result["status"] = "PASS"
    result["application_status"] = "passed"
    validation_log.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return revised

CLAUSE_BOUNDARY = re.compile(r"(?<=[،,:؛;—–…])\s+")
MAX_OVERLONG_SENTENCES = 30      # more than this is a systemic text problem, not a stray run-on
MAX_WORD_LEVEL_SPLITS = 3        # a split with no punctuation at all is the last resort
LAST_CHUNK_REPAIRS: list[dict] = []


def _pack(parts: list[str], limit: int) -> list[str]:
    pieces, current = [], ""
    for part in parts:
        if current and len(current) + 1 + len(part) > limit:
            pieces.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    if current:
        pieces.append(current)
    return pieces


def split_overlong_sentence(sentence: str, limit: int) -> tuple[list[str], str]:
    """Split a run-on at clause punctuation first, then at word boundaries. Returns (pieces, kind)."""
    clauses = [part.strip() for part in CLAUSE_BOUNDARY.split(sentence) if part.strip()]
    if all(len(clause) <= limit for clause in clauses) and len(clauses) > 1:
        return _pack(clauses, limit), "clause"
    words = sentence.split()
    return _pack(words, limit), "word"


def split_chunks(text: str, limit: int = 2600) -> list[str]:
    """Chunks that respect sentence boundaries.

    A sentence longer than the limit is repaired locally instead of stopping the episode: it is
    split at clause punctuation (،  :  ؛  —  …), and only as a last resort at a word boundary.
    Every repair is recorded in ``LAST_CHUNK_REPAIRS``; a systemic problem still fails closed.
    """
    LAST_CHUNK_REPAIRS.clear()
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip() and not p.lstrip().startswith("#")]
    out, current = [], ""
    for paragraph in paragraphs:
        sentences = [s.strip() for s in re.split(r"(?<=[.!؟؛])\s+", paragraph) if s.strip()]
        expanded: list[str] = []
        for sentence in sentences:
            if len(sentence) <= limit:
                expanded.append(sentence)
                continue
            pieces, kind = split_overlong_sentence(sentence, limit)
            if any(len(piece) > limit for piece in pieces):  # a single unbroken token: nothing safe to cut
                raise RuntimeError(
                    f"TTS_CHUNKING_FAILED: sentence exceeds {limit} characters; refusing to split it "
                    f"({len(sentence)} characters starting {sentence[:60]!r})"
                )
            LAST_CHUNK_REPAIRS.append({"kind": kind, "characters": len(sentence), "pieces": len(pieces),
                                       "starts_with": sentence[:60]})
            words_level = sum(1 for item in LAST_CHUNK_REPAIRS if item["kind"] == "word")
            if len(LAST_CHUNK_REPAIRS) > MAX_OVERLONG_SENTENCES or words_level > MAX_WORD_LEVEL_SPLITS:
                raise RuntimeError(
                    f"TTS_CHUNKING_FAILED: sentence exceeds {limit} characters and cannot be split safely "
                    f"({len(sentence)} characters starting {sentence[:60]!r}; {len(LAST_CHUNK_REPAIRS)} overlong "
                    f"sentences, {words_level} without any punctuation)"
                )
            expanded.extend(pieces)
        for sentence in expanded:
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
    models = list(dict.fromkeys(model for model in models if model))
    if SETTINGS.get("cost_policy") != "free_or_preapproved_only":
        raise RuntimeError("BLOCKED_COST: unsupported TTS cost policy")
    approved = set(SETTINGS.get("approved_tts_models") or [])
    unapproved = [model for model in models if model not in approved]
    if unapproved:
        raise RuntimeError(f"BLOCKED_COST: unapproved TTS models: {unapproved}")
    return models


class TtsEngineExhausted(RuntimeError):
    """The locked voice engine cannot continue this episode (quota or retired)."""

    def __init__(self, engine: str, reason: str):
        super().__init__(f"BLOCKED_COST_OR_FREE_LIMIT: TTS engine {engine} exhausted: {reason}")
        self.engine = engine


def gemini_tts_one(text: str, out: Path, model: str):
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
                if exc.code == 404:
                    raise TtsEngineExhausted(model, "model is not available (HTTP 404)") from exc
                if exc.code == 429:
                    quota_kind = _gemini_tts_quota_kind(body)
                    if quota_kind == "daily":
                        raise TtsEngineExhausted(model, "daily free quota exhausted") from exc
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
                    raise TtsEngineExhausted(model, "rate limit persisted") from exc
                if exc.code in (401, 403):
                    raise RuntimeError(f"Gemini TTS authentication failed with HTTP {exc.code}") from exc
                raise RuntimeError(f"Gemini TTS model {model} failed with HTTP {exc.code}") from exc
        if result is None:
            raise RuntimeError("Gemini TTS returned no result")
        audio = base64.b64decode(_gemini_audio_data(result), validate=True)
        if len(audio) <= 1024 or not audio.startswith(b"RIFF"):
            raise RuntimeError("Gemini TTS returned invalid or empty WAV audio")
        wav_path.write_bytes(audio)
        run([ffmpeg(), "-y", "-i", str(wav_path), "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
        return model
    finally:
        wav_path.unlink(missing_ok=True)


TTS_USAGE_PATH = ROOT / "config" / "tts_usage.json"
TTS_QUOTA_STATE_PATH = ROOT / "config" / "tts_quota_state.json"
GOOGLE_CLOUD_PREFIX = "google-cloud:"


def _google_cloud_config() -> dict:
    config = SETTINGS.get("google_cloud_tts") or {}
    return config if isinstance(config, dict) else {}


def _google_cloud_voices() -> list[dict]:
    config = _google_cloud_config()
    if not config.get("enabled") or not SECRETS.get("GOOGLE_CLOUD_TTS_API_KEY"):
        return []
    voices = [voice for voice in config.get("voices") or [] if voice.get("name") and voice.get("tier")]
    approved = set(SETTINGS.get("approved_tts_models") or [])
    unapproved = [v["name"] for v in voices if GOOGLE_CLOUD_PREFIX + v["name"] not in approved]
    if SETTINGS.get("cost_policy") != "free_or_preapproved_only" or unapproved:
        raise RuntimeError(f"BLOCKED_COST: unapproved Google Cloud TTS voices {unapproved or '(cost policy)'}")
    return voices


def _usage_month() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")


def google_cloud_usage(tier: str) -> int:
    usage = json.loads(TTS_USAGE_PATH.read_text(encoding="utf-8")) if TTS_USAGE_PATH.exists() else {}
    return int(usage.get(_usage_month(), {}).get(tier, 0))


def google_cloud_remaining(tier: str) -> int:
    cap = int((_google_cloud_config().get("monthly_char_caps") or {}).get(tier, 0))
    return max(0, cap - google_cloud_usage(tier))


def _record_google_cloud_usage(tier: str, characters: int) -> None:
    usage = json.loads(TTS_USAGE_PATH.read_text(encoding="utf-8")) if TTS_USAGE_PATH.exists() else {}
    month = usage.setdefault(_usage_month(), {})
    month[tier] = int(month.get(tier, 0)) + characters
    TTS_USAGE_PATH.write_text(json.dumps(usage, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _split_utf8(text: str, limit: int) -> list[str]:
    """Split text on sentence boundaries into pieces of at most `limit` UTF-8 bytes."""
    pieces, current = [], ""
    for sentence in re.split(r"(?<=[.!؟?،\n])\s+", text):
        candidate = f"{current} {sentence}" if current else sentence
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current:
            pieces.append(current)
        current = ""
        words = sentence.split(" ")
        for word in words:
            candidate = f"{current} {word}" if current else word
            if len(candidate.encode("utf-8")) <= limit:
                current = candidate
            else:
                if current:
                    pieces.append(current)
                current = word
    if current:
        pieces.append(current)
    return [piece.strip() for piece in pieces if piece.strip()]


def google_cloud_tts_one(text: str, out: Path, voice: dict):
    api_key = SECRETS.get("GOOGLE_CLOUD_TTS_API_KEY", "")
    if not api_key:
        raise RuntimeError("GOOGLE_CLOUD_TTS_API_KEY is missing")
    tier, name = voice["tier"], voice["name"]
    engine = GOOGLE_CLOUD_PREFIX + name
    # Hard free-tier guard: never send a request that would cross the monthly cap.
    if len(text) > google_cloud_remaining(tier):
        raise TtsEngineExhausted(engine, f"monthly {tier} character cap reached")
    config = _google_cloud_config()
    parts = []
    try:
        for index, piece in enumerate(_split_utf8(text, int(config.get("max_request_bytes", 4500)))):
            body = json.dumps({
                "input": {"text": piece},
                "voice": {"languageCode": config.get("language_code", "ar-XA"), "name": name},
                "audioConfig": {"audioEncoding": "MP3", "speakingRate": float(config.get("speaking_rate", 1.0))},
            }, ensure_ascii=False).encode("utf-8")
            request = urllib.request.Request(
                "https://texttospeech.googleapis.com/v1/text:synthesize",
                data=body,
                method="POST",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json; charset=utf-8"},
            )
            try:
                with urllib.request.urlopen(request, timeout=240) as response:
                    result = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                if exc.code in (402, 429):
                    raise TtsEngineExhausted(engine, f"HTTP {exc.code}") from exc
                raise RuntimeError(f"Google Cloud TTS {name} failed with HTTP {exc.code}") from exc
            _record_google_cloud_usage(tier, len(piece))
            part = out.with_suffix(f".part{index:03d}.mp3")
            part.write_bytes(base64.b64decode(result["audioContent"], validate=True))
            parts.append(part)
        listing = out.with_suffix(".parts.txt")
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c:a", "libmp3lame", "-b:a", "160k", str(out)])
        listing.unlink(missing_ok=True)
        return engine
    finally:
        for part in parts:
            part.unlink(missing_ok=True)


GEMINI_VOICE = "gemini-narrator"


def tts_cloud_engines() -> list[str]:
    """Emergency Google Cloud voices, in order."""
    return [GOOGLE_CLOUD_PREFIX + v["name"] for v in _google_cloud_voices()]


def quota_day() -> str:
    """Gemini daily quotas reset at midnight Pacific time."""
    try:
        from zoneinfo import ZoneInfo
        return dt.datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
    except Exception:
        return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=8)).date().isoformat()


def _quota_state() -> dict:
    if not TTS_QUOTA_STATE_PATH.exists():
        return {}
    try:
        value = json.loads(TTS_QUOTA_STATE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def _save_quota_state(value: dict) -> None:
    TTS_QUOTA_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = TTS_QUOTA_STATE_PATH.with_suffix(TTS_QUOTA_STATE_PATH.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(TTS_QUOTA_STATE_PATH)


def gemini_quota_exhausted_today() -> bool:
    policy = SETTINGS.get("voice_policy") or {}
    if not policy.get("skip_exhausted_gemini_for_quota_day", True):
        return False
    return _quota_state().get("gemini_exhausted_quota_day") == quota_day()


def mark_gemini_quota_exhausted(reason: str) -> None:
    state = _quota_state()
    state.update(
        gemini_exhausted_quota_day=quota_day(),
        reason=reason,
        updated_at=dt.datetime.now(dt.timezone.utc).isoformat(),
    )
    _save_quota_state(state)


def clear_expired_gemini_quota_state() -> None:
    state = _quota_state()
    exhausted_day = state.get("gemini_exhausted_quota_day")
    if exhausted_day and exhausted_day != quota_day():
        _save_quota_state({})


async def tts_one(text: str, out: Path, engine: str = ""):
    from error_classifier import classify_error, ErrorCategory
    provider = SETTINGS.get("tts_provider", "edge").lower()
    if provider in ("fish", "munsit", "gemini"):
        last_error = None
        for attempt in range(3):
            try:
                if provider == "gemini" and engine.startswith(GOOGLE_CLOUD_PREFIX):
                    voice = next(v for v in _google_cloud_voices() if GOOGLE_CLOUD_PREFIX + v["name"] == engine)
                    return await asyncio.to_thread(google_cloud_tts_one, text, out, voice)
                if provider == "gemini":
                    return await asyncio.to_thread(gemini_tts_one, text, out, engine or _gemini_tts_models()[0])
                worker = {"fish": fish_tts_one, "munsit": munsit_tts_one}[provider]
                return await asyncio.to_thread(worker, text, out)
            except Exception as exc:
                cat = classify_error(exc)
                if cat != ErrorCategory.TRANSIENT_RETRYABLE or "BLOCKED_COST" in str(exc):
                    raise exc
                last_error = exc
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        raise RuntimeError(f"{provider} transient request failed after 3 attempts: {type(last_error).__name__}") from last_error
    await edge_tts_one(text, out)
    return "edge"


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

def background_volume_percent() -> float:
    return round(float(SETTINGS.get("background_audio_volume", 0.03)) * 100, 2)


def mix_background_audio(narration: Path, output: Path) -> None:
    import background_audio
    background = background_audio.ensure_background_audio(ROOT, SETTINGS.get("background_audio_file")).resolve()
    volume = float(SETTINGS.get("background_audio_volume", 0.03))
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
    run_summary.stage("prepare")
    metadata_path = story / "15_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required_gates = (
        "topic_gate_status", "grounded_research_status", "source_validation_status",
        "language_gate_status", "religion_gate_status", "sleep_gate_status",
        "fact_check_status", "second_fact_check_status",
    )
    failed = [name for name in required_gates if metadata.get(name) != "passed"]
    if failed or metadata.get("fact_corrections_applied") is not True:
        raise RuntimeError(f"TTS_HARD_GATE_FAILED: missing passes: {failed}")
    clean_path=story/"05_story_clean.md"
    clean_story=clean_path.read_text(encoding="utf-8").strip()
    extension=story/"draft_extension.md"
    extension_marker="<!-- MERGED_DRAFT_EXTENSION -->"
    if extension.exists() and extension_marker not in clean_story and arabic_words(clean_story) < 5000:
        clean_story += f"\n\n{extension_marker}\n\n" + extension.read_text(encoding="utf-8").strip()
        clean_path.write_text(clean_story+"\n",encoding="utf-8")
    logs = story / "logs"
    logs.mkdir(exist_ok=True)
    raw_source_hash = hashlib.sha256(clean_story.encode("utf-8")).hexdigest()
    rules_hash = file_sha256(TTS_RULES_PATH)
    prepare_manifest_path = logs / "tts_prepare.json"
    prepare_manifest = {}
    if prepare_manifest_path.exists():
        prepare_manifest = json.loads(prepare_manifest_path.read_text(encoding="utf-8"))
    normalized_path = story / "05b_story_tts_normalized.md"
    normalization_reusable = (
        prepare_manifest.get("raw_source_sha256") == raw_source_hash
        and prepare_manifest.get("normalization_version") == TTS_NORMALIZATION_VERSION
        and prepare_manifest.get("normalization_rules_sha256") == rules_hash
        and prepare_manifest.get("tts_normalization_status") == "passed"
        and prepare_manifest.get("speech_safe_validation_status") == "passed"
        and normalized_path.exists()
        and not speech_safe_violations(normalized_path.read_text(encoding="utf-8"))
    )
    if normalization_reusable:
        normalized_story = normalized_path.read_text(encoding="utf-8").strip()
    else:
        normalized_story = normalize_for_tts(clean_story, story)
        normalized_path.write_text(normalized_story + "\n", encoding="utf-8")
    require_speech_safe(normalized_story, stage="PRE_DIACRITIZATION")
    source_hash = hashlib.sha256(normalized_story.encode("utf-8")).hexdigest()
    annotated_path=story/"06_story_tts_annotated.md"
    reusable = (
        prepare_manifest.get("source_sha256") == source_hash
        and normalization_reusable
        and prepare_manifest.get("version") == DIACRITIZATION_VERSION
        and prepare_manifest.get("diacritization_status") == "passed"
        and prepare_manifest.get("pronunciation_validation_status") == "passed"
        and prepare_manifest.get("tts_preparation_status") == "passed"
        and prepare_manifest.get("direction_tags_present") is False
        and annotated_path.exists()
        and diacritic_ratio(annotated_path.read_text(encoding="utf-8")) >= 0.27
        and not DIRECTION_RE.findall(annotated_path.read_text(encoding="utf-8"))
    )
    if reusable:
        annotated = annotated_path.read_text(encoding="utf-8").strip()
    else:
        annotated = diacritize_and_annotate(normalized_story, logs / "diacritics_cache")
        annotated = pronunciation_validate(normalized_story, annotated, story)
        annotated_path.write_text(annotated+"\n",encoding="utf-8")
        prepare_manifest_path.write_text(json.dumps({
            "raw_source_sha256": raw_source_hash,
            "source_sha256": source_hash,
            "version": DIACRITIZATION_VERSION,
            "normalization_version": TTS_NORMALIZATION_VERSION,
            "normalization_rules_sha256": rules_hash,
            "tts_normalization_status": "passed",
            "speech_safe_validation_status": "passed",
            "diacritic_ratio": round(diacritic_ratio(annotated), 4),
            "diacritization_status": "passed",
            "pronunciation_validation_status": "passed",
            "tts_preparation_status": "passed",
            "direction_tags_present": False,
            "review_passes": int(SETTINGS.get("diacritization_review_passes", 2)),
            "tts_generation_speed": float(SETTINGS.get("tts_generation_speed", 1.0)),
            "post_processing_tempo": float(SETTINGS.get("post_tts_tempo", 1.0)),
        }, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    clean = api_text(annotated)
    require_pausal_sentence_endings(clean)
    require_speech_safe(clean, stage="FINAL_TTS_TEXT")
    api_faithful, _ = lexical_fidelity(normalized_story, clean)
    if not api_faithful:
        raise RuntimeError("TTS API text differs lexically from the normalized speech story")
    (story / "07_story_tts_api.md").write_text(clean + "\n", encoding="utf-8")
    metadata.update(
        tts_normalization_status="passed",
        speech_safe_validation_status="passed",
        diacritization_status="passed",
        tts_final_diacritics_status="pronunciation_accurate_pausal",
        pronunciation_validation_status="passed",
        tts_preparation_status="passed",
    )
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    provider = SETTINGS.get("tts_provider", "edge").lower()
    chunk_limit = int(
        SETTINGS.get("gemini_tts_chunk_char_limit", 4200)
        if provider == "gemini"
        else SETTINGS.get("tts_chunk_char_limit", 2600)
    )
    chunks = split_chunks(clean, limit=chunk_limit)
    if LAST_CHUNK_REPAIRS:
        print(f"TTS_CHUNKING_REPAIRS={len(LAST_CHUNK_REPAIRS)} {json.dumps(LAST_CHUNK_REPAIRS, ensure_ascii=False)[:400]}", flush=True)
        (story / "logs").mkdir(exist_ok=True)
        (story / "logs" / "tts_chunking_repairs.json").write_text(
            json.dumps(LAST_CHUNK_REPAIRS, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    maximum_chunks = int(SETTINGS.get("max_tts_chunks_per_episode", 40))
    if len(chunks) > maximum_chunks:
        raise RuntimeError(
            f"BLOCKED_COST: TTS requires {len(chunks)} chunks, limit is {maximum_chunks}"
        )
    tdir = story / "tts_chunks"; tdir.mkdir(exist_ok=True)
    for old_chunk in tdir.glob("*.txt"):
        old_chunk.unlink()
    for i, chunk in enumerate(chunks, 1):
        (tdir / f"{i:03}.txt").write_text(chunk + "\n", encoding="utf-8")
    print(f"Prepared {len(chunks)} chunks, {arabic_words(clean)} Arabic words, diacritics={diacritic_ratio(annotated):.3f}")

async def regenerate_audio_chunk_for_qc(
    src: Path,
    out: Path,
    provider: str,
    voice: str,
    chunk_models: dict[str, str],
) -> str:
    """Regenerate only one transcript-mismatched chunk while preserving narrator identity."""
    text = src.read_text(encoding="utf-8")
    out.unlink(missing_ok=True)

    if provider == "gemini":
        if voice.startswith(GOOGLE_CLOUD_PREFIX):
            candidates = [voice]
        else:
            preferred = chunk_models.get(src.name, "")
            candidates = [model for model in dict.fromkeys([preferred, *_gemini_tts_models()]) if model]
        for candidate in candidates:
            try:
                actual_model = await tts_one(text, out, candidate)
                return actual_model or candidate
            except TtsEngineExhausted as exc:
                print(f"{exc}; trying the next same-narrator model for QC repair", flush=True)
        raise RuntimeError(
            f"TTS_WAITING_FOR_QUOTA: cannot regenerate transcript-mismatched chunk {out.name} "
            "with the current narrator"
        )

    actual_model = await tts_one(text, out, voice)
    if actual_model:
        return actual_model
    if provider == "fish":
        return str(SETTINGS.get("fish_model", "s2.1-pro-free"))
    if provider == "munsit":
        return str(SETTINGS.get("munsit_model", "faseeh-v1-preview"))
    return "edge"


async def synthesize(story: Path):
    run_summary.stage("synthesize")
    provider = SETTINGS.get("tts_provider", "edge").lower()
    tts_config = {
        "provider": provider,
        "voice": SETTINGS.get("tts_voice"),
        "rate": SETTINGS.get("tts_rate"),
        "generation_speed": SETTINGS.get("tts_generation_speed"),
        "post_tts_tempo": SETTINGS.get("post_tts_tempo"),
        "diacritization": SETTINGS.get("arabic_diacritization"),
        "final_diacritics": SETTINGS.get("tts_final_diacritics", "pronunciation_accurate_pausal"),
    }
    config_hash = compute_config_hash(tts_config)

    adir = story / "audio_chunks"
    if can_reuse_stage(story, "synthesize", config_hash):
        print(f"Reusing existing valid TTS output for {story.name} (config hash {config_hash[:8]})")
        if adir.exists() and list(adir.glob("*.mp3")):
            validate_audio(story)
            return

    if provider == "fish":
        # Fail before generating any paid/free-tier chunks if the credential
        # points at a different voice than the selected model.
        await asyncio.to_thread(validate_fish_voice)
    elif provider == "munsit":
        await asyncio.to_thread(validate_munsit_voice)
    elif provider == "gemini":
        await asyncio.to_thread(validate_gemini_voice)
    prepare(story)
    run_summary.stage("synthesize")
    tdir, adir = story / "tts_chunks", story / "audio_chunks"
    adir.mkdir(exist_ok=True)
    hash_path = story / "logs" / "tts_chunk_hashes.json"
    old_hashes = json.loads(hash_path.read_text(encoding="utf-8")) if hash_path.exists() else {}
    model_path = story / "logs" / "tts_chunk_models.json"
    old_models = json.loads(model_path.read_text(encoding="utf-8")) if model_path.exists() else {}
    text_sources = sorted(tdir.glob("*.txt"))
    valid_names = {src.name for src in text_sources}
    new_hashes = {name: value for name, value in old_hashes.items() if name in valid_names}
    chunk_models = {name: value for name, value in old_models.items() if name in valid_names}
    # Permanent voice rules (see voice_policy in settings):
    # - Gemini TTS models share one narrator voice and may alternate per chunk.
    # - When every Gemini model is out of quota, Google Cloud takes over
    #   immediately and regenerates the full narration with one voice.
    # - The exhausted Gemini quota day is shared across episodes, so later stories
    #   do not waste requests retrying Gemini before its next quota reset.
    # - Switching discards all Gemini chunks so one episode never mixes narrators.
    state_path = story / "logs" / "tts_voice_state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    voice = state.get("voice", GEMINI_VOICE) if provider == "gemini" else ""
    if voice != GEMINI_VOICE and voice not in tts_cloud_engines():
        voice = GEMINI_VOICE if provider == "gemini" else ""
    def save_state() -> None:
        if provider == "gemini":
            state["voice"] = voice
            state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def reset_chunks() -> None:
        nonlocal old_hashes, old_models, new_hashes, chunk_models
        for stale in adir.glob("*.mp3"):
            stale.unlink()
        old_hashes, old_models, new_hashes, chunk_models = {}, {}, {}, {}
        hash_path.unlink(missing_ok=True)
        model_path.unlink(missing_ok=True)

    if provider == "gemini" and voice == GEMINI_VOICE:
        clear_expired_gemini_quota_state()
        if gemini_quota_exhausted_today():
            cloud_engines = tts_cloud_engines()
            if not cloud_engines:
                raise RuntimeError(
                    "TTS_WAITING_FOR_QUOTA: Gemini quota is exhausted for this quota day and no "
                    "Google Cloud fallback is available"
                )
            print(
                f"Gemini quota is already exhausted for {quota_day()}; starting this episode with "
                f"{cloud_engines[0]}",
                flush=True,
            )
            voice = cloud_engines[0]
            reset_chunks()

    while True:
        save_state()
        try:
            if voice.startswith(GOOGLE_CLOUD_PREFIX):
                cloud = next(v for v in _google_cloud_voices() if GOOGLE_CLOUD_PREFIX + v["name"] == voice)
                needed = sum(len(src.read_text(encoding="utf-8")) for src in text_sources)
                if needed > google_cloud_remaining(cloud["tier"]):
                    raise TtsEngineExhausted(voice, f"episode needs {needed} characters; monthly cap would be exceeded")
            gemini_models = _gemini_tts_models() if voice == GEMINI_VOICE else []
            for src in text_sources:
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
                    else voice if provider == "gemini"
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
                cached_model = old_models.get(src.name, "")
                allowed_models = set(gemini_models) if voice == GEMINI_VOICE else {provider_model}
                if (
                    out.exists() and out.stat().st_size > 1024 and old_hashes.get(src.name) == source_hash
                    and (cached_model in allowed_models or provider != "gemini")
                ):
                    chunk_models[src.name] = cached_model or provider_model
                    hash_path.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                    model_path.write_text(json.dumps(chunk_models, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                    continue
                if voice == GEMINI_VOICE:
                    actual_model = ""
                    for model in list(gemini_models):
                        try:
                            actual_model = await tts_one(text, out, model)
                            break
                        except TtsEngineExhausted as exc:
                            print(f"{exc}; continuing this chunk with the next Gemini model (same narrator)", flush=True)
                            gemini_models.remove(model)
                    if not actual_model:
                        raise TtsEngineExhausted(GEMINI_VOICE, "every Gemini TTS model is out of quota")
                else:
                    actual_model = await tts_one(text, out, voice)
                chunk_models[src.name] = actual_model or provider_model
                if out.stat().st_size <= 1024: raise RuntimeError(f"empty audio chunk: {out}")
                # Persist progress after every successful chunk so an interrupted run
                # resumes from the next missing/stale chunk instead of starting over.
                hash_path.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                model_path.write_text(json.dumps(chunk_models, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            break
        except TtsEngineExhausted as exc:
            if provider != "gemini":
                raise
            if voice == GEMINI_VOICE:
                mark_gemini_quota_exhausted(str(exc))
                cloud_engines = tts_cloud_engines()
                policy = SETTINGS.get("voice_policy") or {}
                if policy.get("immediate_cloud_fallback", True) and cloud_engines:
                    print(
                        f"Gemini quota exhausted; switching immediately to {cloud_engines[0]} "
                        "and regenerating the whole narration",
                        flush=True,
                    )
                    voice = cloud_engines[0]
                    reset_chunks()
                    continue
                save_state()
                raise RuntimeError(
                    f"TTS_WAITING_FOR_QUOTA: Gemini narrator quota is used up; {len(chunk_models)} of "
                    f"{len(text_sources)} chunks are saved and no immediate cloud fallback is available"
                ) from exc
            cloud_engines = tts_cloud_engines()
            following = cloud_engines[cloud_engines.index(voice) + 1:] if voice in cloud_engines else []
            if not following:
                save_state()
                raise
            print(f"{exc}; regenerating the whole narration with {following[0]}", flush=True)
            voice = following[0]
            reset_chunks()
    valid_stems = {path.stem for path in tdir.glob("*.txt")}
    for stale_audio in adir.glob("*.mp3"):
        if stale_audio.stem not in valid_stems:
            stale_audio.unlink()
    hash_path.write_text(json.dumps(new_hashes, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    model_path.write_text(json.dumps(chunk_models, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    concat = adir / "concat.txt"
    tempo = float(SETTINGS.get("post_tts_tempo", 1.0))
    if tempo != 1.0:
        raise RuntimeError(f"Post-TTS tempo guard failed: expected native 1.0, got {tempo}")
    narration = story / "12_audio_narration.mp3"
    full = story / "12_audio_full.mp3"
    review = story / "13_audio_review.opus"
    qc_repair_limit = max(0, int(SETTINGS.get("audio_qc_chunk_repair_attempts", 2)))
    qc_repair_counts: dict[str, int] = {}

    while True:
        concat.write_text("".join(f"file '{p.as_posix()}'\n" for p in sorted(adir.glob("*.mp3"))), encoding="utf-8")
        run([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", str(concat), "-c:a", "libmp3lame", "-b:a", "160k", str(narration)])
        mix_background_audio(narration, full)
        run([ffmpeg(), "-y", "-i", str(full), "-c:a", "libopus", "-b:a", "48k", str(review)])
        try:
            validate_audio(story)
            break
        except AudioTranscriptMismatch as exc:
            attempts = qc_repair_counts.get(exc.chunk_name, 0)
            if attempts >= qc_repair_limit:
                raise
            qc_repair_counts[exc.chunk_name] = attempts + 1
            src = tdir / f"{exc.chunk_stem}.txt"
            out = adir / exc.chunk_name
            if not src.exists():
                raise RuntimeError(f"AUDIO_QC_FAILED: source text missing for {exc.chunk_name}") from exc
            print(
                f"Audio QC targeted repair {qc_repair_counts[exc.chunk_name]}/{qc_repair_limit}: "
                f"regenerating only {exc.chunk_name}",
                flush=True,
            )
            actual_model = await regenerate_audio_chunk_for_qc(src, out, provider, voice, chunk_models)
            chunk_models[src.name] = actual_model
            model_path.write_text(json.dumps(chunk_models, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

    print("Audio duration", duration(full))

    output_hashes = {
        "12_audio_narration.mp3": compute_file_hash(narration),
        "12_audio_full.mp3": compute_file_hash(full),
        "13_audio_review.opus": compute_file_hash(review),
    }
    record_stage(story, "synthesize", input_sha256=config_hash, output_hashes=output_hashes)


AUDIO_QC_ORTHOGRAPHY_TABLE = str.maketrans({
    # ASR frequently returns phonetic Arabic spelling rather than editorial spelling.
    # These forms do not change the spoken lexical identity we are validating.
    "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
    "ة": "ه",
    "ى": "ي",
    "ؤ": "و",
    "ئ": "ي",
})


def _audio_qc_tokens(text: str) -> list[str]:
    """Tokenize Arabic for spoken-fidelity QC, normalizing ASR-only spelling variants."""
    bare = DIACRITICS_RE.sub("", text).replace("ـ", "").translate(AUDIO_QC_ORTHOGRAPHY_TABLE)
    return re.findall(r"[\u0621-\u064A]+|\d+", bare)


def _audio_qc_diff(source_tokens: list[str], transcript_tokens: list[str], limit: int = 24) -> list[dict]:
    """Return a bounded, non-secret token diff for diagnosing transcript mismatches."""
    matcher = difflib.SequenceMatcher(None, source_tokens, transcript_tokens, autojunk=False)
    changes = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changes.append({
            "tag": tag,
            "source_range": [i1, i2],
            "transcript_range": [j1, j2],
            "source": source_tokens[i1:i2][:20],
            "transcript": transcript_tokens[j1:j2][:20],
        })
        if len(changes) >= limit:
            break
    return changes


def validate_audio(story: Path) -> dict:
    text_chunks = sorted((story / "tts_chunks").glob("*.txt"))
    audio_chunks = sorted((story / "audio_chunks").glob("*.mp3"))
    if [item.stem for item in text_chunks] != [item.stem for item in audio_chunks]:
        raise RuntimeError("AUDIO_QC_FAILED: text/audio chunk sequence mismatch")
    if not audio_chunks:
        raise RuntimeError("AUDIO_QC_FAILED: no audio chunks")
    hashes = []
    chunk_durations = []
    transcript_checks = []
    audio_signatures = set()
    transcript_qc = bool(SETTINGS.get("audio_transcript_qc", True))
    audio_qc_model = str(SETTINGS.get("audio_qc_model", SETTINGS.get("llm_model", "")))
    if transcript_qc and (
        SETTINGS.get("cost_policy") != "free_or_preapproved_only"
        or audio_qc_model not in set(SETTINGS.get("approved_llm_models") or [])
    ):
        raise RuntimeError(f"BLOCKED_COST: unapproved audio QC model {audio_qc_model}")

    logs = story / "logs"
    logs.mkdir(exist_ok=True)
    cache_path = logs / "audio_qc_chunk_cache.json"
    try:
        transcript_cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    except (json.JSONDecodeError, OSError):
        transcript_cache = {}
    if not isinstance(transcript_cache, dict):
        transcript_cache = {}
    valid_audio_names = {chunk.name for chunk in audio_chunks}
    transcript_cache = {name: value for name, value in transcript_cache.items() if name in valid_audio_names}

    for chunk in audio_chunks:
        if chunk.stat().st_size <= 1024:
            raise RuntimeError(f"AUDIO_QC_FAILED: empty chunk {chunk.name}")
        probe = run([ffmpeg(), "-v", "error", "-i", str(chunk), "-f", "null", "-"], check=False)
        if probe.returncode:
            raise RuntimeError(f"AUDIO_QC_FAILED: undecodable chunk {chunk.name}")
        stream_probe = subprocess.run([
            ffprobe(), "-v", "error", "-select_streams", "a:0",
            "-show_entries", "stream=codec_name,sample_rate,channels", "-of", "json", str(chunk),
        ], capture_output=True, text=True, check=True)
        stream = (json.loads(stream_probe.stdout).get("streams") or [{}])[0]
        audio_signatures.add((stream.get("codec_name"), stream.get("sample_rate"), stream.get("channels")))
        chunk_durations.append(round(duration(chunk), 3))
        chunk_sha = hashlib.sha256(chunk.read_bytes()).hexdigest()
        hashes.append(chunk_sha)
        if transcript_qc:
            source_path = next(item for item in text_chunks if item.stem == chunk.stem)
            source = source_path.read_text(encoding="utf-8")
            source_sha = hashlib.sha256(source.encode("utf-8")).hexdigest()
            cached = transcript_cache.get(chunk.name) if isinstance(transcript_cache.get(chunk.name), dict) else {}
            if (
                cached.get("status") == "passed"
                and cached.get("audio_sha256") == chunk_sha
                and cached.get("source_sha256") == source_sha
            ):
                similarity = float(cached.get("similarity", 1.0))
                length_ratio = float(cached.get("length_ratio", 1.0))
                transcript_checks.append({
                    "chunk": chunk.name,
                    "similarity": round(similarity, 4),
                    "length_ratio": round(length_ratio, 4),
                    "cached": True,
                })
                continue

            transcript = transcribe_audio_for_qc(chunk, audio_qc_model)
            source_tokens = _audio_qc_tokens(source)
            transcript_tokens = _audio_qc_tokens(transcript)
            # Natural-language token streams are long and intentionally repetitive. Disable
            # SequenceMatcher's autojunk heuristic so common Arabic words remain valid anchors.
            similarity = difflib.SequenceMatcher(None, source_tokens, transcript_tokens, autojunk=False).ratio()
            length_ratio = len(transcript_tokens) / max(len(source_tokens), 1)
            transcript_checks.append({
                "chunk": chunk.name, "similarity": round(similarity, 4),
                "length_ratio": round(length_ratio, 4), "cached": False,
            })
            if similarity < float(SETTINGS.get("audio_transcript_similarity_min", 0.82)) or not 0.8 <= length_ratio <= 1.2:
                transcript_cache.pop(chunk.name, None)
                cache_path.write_text(json.dumps(transcript_cache, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
                failure = {
                    "chunk": chunk.name,
                    "audio_sha256": chunk_sha,
                    "source_sha256": source_sha,
                    "similarity": round(similarity, 6),
                    "length_ratio": round(length_ratio, 6),
                    "source_token_count": len(source_tokens),
                    "transcript_token_count": len(transcript_tokens),
                    "transcript": transcript,
                    "diff": _audio_qc_diff(source_tokens, transcript_tokens),
                }
                (logs / f"audio_qc_failure_{chunk.stem}.json").write_text(
                    json.dumps(failure, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                raise AudioTranscriptMismatch(chunk.name, similarity, length_ratio)
            transcript_cache[chunk.name] = {
                "status": "passed",
                "audio_sha256": chunk_sha,
                "source_sha256": source_sha,
                "similarity": round(similarity, 6),
                "length_ratio": round(length_ratio, 6),
            }
            cache_path.write_text(json.dumps(transcript_cache, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    duplicates = [index + 2 for index in range(len(hashes) - 1) if hashes[index] == hashes[index + 1]]
    if duplicates:
        raise RuntimeError(f"AUDIO_QC_FAILED: duplicate adjacent chunks {duplicates}")
    if len(audio_signatures) != 1:
        raise RuntimeError(f"AUDIO_QC_FAILED: inconsistent chunk audio format {audio_signatures}")
    full = story / "12_audio_full.mp3"
    full_duration = duration(full)
    minimum = float(SETTINGS["target_min_seconds"])
    maximum = float(SETTINGS["target_max_seconds"])
    if not minimum <= full_duration <= maximum:
        raise RuntimeError(
            f"AUDIO_QC_FAILED: duration {full_duration:.1f}s outside {minimum:.0f}-{maximum:.0f}s"
        )
    silence = run([
        ffmpeg(), "-hide_banner", "-i", str(full), "-af", "silencedetect=noise=-45dB:d=8",
        "-f", "null", "-",
    ], check=False)
    long_silences = [float(value) for value in re.findall(r"silence_duration:\s*([0-9.]+)", silence.stderr)]
    if long_silences:
        raise RuntimeError("AUDIO_QC_FAILED: unnatural silence >= 8 seconds")
    result = {
        "status": "PASS",
        "chunks": len(audio_chunks),
        "chunk_durations": chunk_durations,
        "full_duration_seconds": round(full_duration, 3),
        "long_silences_seconds": [],
        "transcript_checks": transcript_checks,
    }
    (logs / "audio_qc.json").write_text(json.dumps(result, indent=2)+"\n", encoding="utf-8")
    metadata_path = story / "15_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(audio_generation_status="passed", audio_qc_status="passed")
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return result

QC_RETRY_DELAYS = (10, 30, 60, 120)
QC_TRANSIENT_CODES = {429, 500, 502, 503, 504}


def _qc_models(primary: str) -> list[str]:
    approved = set(SETTINGS.get("approved_llm_models") or [])
    chain = [primary, SETTINGS.get("llm_model", ""), *(SETTINGS.get("llm_fallback_models") or [])]
    return [model for model in dict.fromkeys(chain) if model and model in approved]


def _qc_request_with_fallback(request: urllib.request.Request, primary: str) -> dict:
    """Transcript QC stays mandatory, but a busy or exhausted model is retried
    with backoff and then replaced by the next approved model."""
    last_error: Exception | None = None
    for model in _qc_models(primary):
        model_request = urllib.request.Request(
            request.full_url.replace(f"/models/{primary}:", f"/models/{model}:"),
            data=request.data,
            headers=dict(request.header_items()),
            method="POST",
        )
        for attempt in range(len(QC_RETRY_DELAYS) + 1):
            try:
                with urllib.request.urlopen(model_request, timeout=240) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 404 or exc.code not in QC_TRANSIENT_CODES:
                    break
                body = exc.read().decode("utf-8", errors="replace").lower()
                if exc.code == 429 and ("perday" in body.replace("_", "") or "per day" in body):
                    break
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
            if attempt < len(QC_RETRY_DELAYS):
                print(f"Audio QC transcript {model} temporarily unavailable; retrying in {QC_RETRY_DELAYS[attempt]}s", flush=True)
                time.sleep(QC_RETRY_DELAYS[attempt])
        print(f"Audio QC transcript model {model} unavailable; trying the next approved model", flush=True)
    raise RuntimeError(
        f"AUDIO_QC_FAILED: transcript provider unavailable: {type(last_error).__name__ if last_error else 'no approved model'}"
    ) from last_error


def transcribe_audio_for_qc(path: Path, model: str) -> str:
    api_key = SECRETS.get("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("AUDIO_QC_FAILED: GEMINI_API_KEY is missing for transcript validation")
    payload = {
        "contents": [{"role": "user", "parts": [
            {"text": "Transcribe this Arabic narration verbatim. Output only the spoken words, without commentary or timestamps."},
            {"inline_data": {"mime_type": "audio/mpeg", "data": base64.b64encode(path.read_bytes()).decode("ascii")}},
        ]}],
        "generationConfig": {"temperature": 0, "maxOutputTokens": 8192},
    }
    request = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    result = _qc_request_with_fallback(request, model)
    transcript = "".join(
        part.get("text", "")
        for part in (result.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    ).strip()
    if not transcript:
        raise RuntimeError("AUDIO_QC_FAILED: empty transcript")
    return transcript

def background(story: Path, force: bool = False):
    run_summary.stage("background")
    output = story / "10_background.png"
    thumbnail = story / "11_thumbnail_base.jpg"
    prompt_file = story / "08_image_prompt_en.txt"
    prompt = prompt_file.read_text(encoding="utf-8").strip() if prompt_file.exists() else ""
    prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest() if prompt else ""
    metadata_path = story / "15_metadata.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}

    thumbnail_text = str(metadata.get("thumbnail_text", "")).strip()
    thumbnail_input_hash = ""
    if thumbnail_text:
        import canva_thumbnail
        effective_thumbnail_prompt = canva_thumbnail.build_background_prompt(
            str(metadata.get("thumbnail_prompt", "")).strip(),
            prompt,
        )
        thumbnail_input_hash = canva_thumbnail.input_identity(
            thumbnail_text,
            effective_thumbnail_prompt,
        )

    stage_input_hash = (
        compute_config_hash({
            "image_prompt_sha256": prompt_hash,
            "thumbnail_input_sha256": thumbnail_input_hash,
        })
        if thumbnail_input_hash
        else prompt_hash
    )

    if not force and can_reuse_stage(story, "background", stage_input_hash):
        print(
            f"Reusing existing valid background image and thumbnail for {story.name} "
            f"(stage hash {stage_input_hash[:8]})"
        )
        return

    if (
        not force and output.exists() and thumbnail.exists() and output.stat().st_size > 250_000
        and (metadata.get("image_generation_status") == "passed" or not metadata_path.exists())
        and (not prompt_hash or metadata.get("image_prompt_sha256") == prompt_hash)
        and (not thumbnail_input_hash or metadata.get("thumbnail_input_sha256") == thumbnail_input_hash)
        and (not metadata.get("image_sha256") or metadata.get("image_sha256") == file_sha256(output))
        and (not metadata.get("thumbnail_sha256") or metadata.get("thumbnail_sha256") == file_sha256(thumbnail))
    ):
        print(f"Reusing existing valid background image and thumbnail for {story.name}")
        return

    generator = ROOT / "scripts" / "image_generate.py"
    run([sys.executable, str(generator), str(story)])

    output_hashes = {
        "10_background.png": compute_file_hash(output),
        "11_thumbnail_base.jpg": compute_file_hash(thumbnail),
    }
    record_stage(story, "background", input_sha256=stage_input_hash, output_hashes=output_hashes)
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
    run_summary.stage("render")
    audio = story / "12_audio_full.mp3"
    out = story / "14_video_final.mp4"
    bg = story / "10_background.png"

    render_config = {
        "bg_hash": compute_file_hash(bg),
        "audio_hash": compute_file_hash(audio),
    }
    render_hash = compute_config_hash(render_config)

    if can_reuse_stage(story, "render", render_hash):
        print(f"Reusing existing valid video render for {story.name} (render hash {render_hash[:8]})")
        return

    if out.exists() and out.stat().st_size > 1_000_000 and abs(duration(audio) - duration(out)) <= 2.5:
        print(f"Reusing existing valid video render for {story.name}")
        return
    exact_duration = duration(audio)
    codec = video_codec()
    run([ffmpeg(),"-y","-loop","1","-framerate","1","-i",str(bg),"-i",str(audio),"-vf","format=yuv420p","-r","2",*codec,"-c:a","aac","-b:a","128k","-t",f"{exact_duration:.3f}","-movflags","+faststart",str(out)])

    output_hashes = {
        "14_video_final.mp4": compute_file_hash(out),
    }
    record_stage(story, "render", input_sha256=render_hash, output_hashes=output_hashes)

def validate(story: Path):
    run_summary.stage("validate")
    required=["01_title.txt","02_description.txt","03_sources.md","04_research_notes.md","05_story_clean.md","05b_story_tts_normalized.md","06_story_tts_annotated.md","07_story_tts_api.md","10_background.png","11_thumbnail_base.jpg","12_audio_narration.mp3","12_audio_full.mp3","13_audio_review.opus","14_video_final.mp4","15_metadata.json","16_fact_check.md","17_status.md"]
    initial_meta = json.loads((story / "15_metadata.json").read_text(encoding="utf-8")) if (story / "15_metadata.json").exists() else {}
    packaged_episode = bool(initial_meta.get("youtube_packaging_status"))
    if packaged_episode:
        required.append("08_thumbnail_prompt_en.txt")
    missing=[x for x in required if not (story/x).exists()]
    if missing: raise RuntimeError("Missing: "+", ".join(missing))
    if packaged_episode:
        final_title = (story / "01_title.txt").read_text(encoding="utf-8").strip()
        thumbnail_prompt = (story / "08_thumbnail_prompt_en.txt").read_text(encoding="utf-8").strip()
        thumbnail_text = str(initial_meta.get("thumbnail_text", "")).strip()
        if final_title != initial_meta.get("final_youtube_title") or final_title != initial_meta.get("title"):
            raise RuntimeError("YOUTUBE_PACKAGING_VALIDATION_FAILED: title file and metadata differ")
        if thumbnail_prompt != str(initial_meta.get("thumbnail_prompt", "")).strip():
            raise RuntimeError("YOUTUBE_PACKAGING_VALIDATION_FAILED: thumbnail prompt file and metadata differ")
        if not 2 <= len(re.findall(r"[\u0621-\u064A]+", thumbnail_text)) <= 4:
            raise RuntimeError("YOUTUBE_PACKAGING_VALIDATION_FAILED: thumbnail text must contain 2-4 Arabic words")
        if initial_meta.get("thumbnail_text_render_status") != "passed":
            raise RuntimeError("YOUTUBE_PACKAGING_VALIDATION_FAILED: exact thumbnail text was not rendered")
    annotated_text=(story/"06_story_tts_annotated.md").read_text(encoding="utf-8")
    api_tts_text=(story/"07_story_tts_api.md").read_text(encoding="utf-8")
    clean_story_text=(story/"05_story_clean.md").read_text(encoding="utf-8")
    normalized_story_text=(story/"05b_story_tts_normalized.md").read_text(encoding="utf-8")
    require_speech_safe(normalized_story_text, stage="VALIDATE_NORMALIZED_STORY")
    require_speech_safe(api_tts_text, stage="VALIDATE_FINAL_TTS")
    faithful, lexical_corrections=lexical_fidelity(normalized_story_text, annotated_text)
    if not faithful:
        raise RuntimeError("TTS annotated text differs lexically from the clean story")
    current_diacritic_ratio=diacritic_ratio(annotated_text)
    if current_diacritic_ratio < 0.27:
        raise RuntimeError(f"Arabic TTS text is not fully diacritized: ratio={current_diacritic_ratio:.3f}")
    human_tags=DIRECTION_RE.findall(annotated_text)
    api_tags=DIRECTION_RE.findall(api_tts_text)
    if human_tags or api_tags:
        raise RuntimeError(f"Performance tags are forbidden in TTS files: {(human_tags + api_tags)[:3]}")
    api_faithful, _ = lexical_fidelity(normalized_story_text, api_tts_text)
    if not api_faithful:
        raise RuntimeError("API TTS text differs lexically from the clean story")
    quality_errors=diacritization_quality_errors(annotated_text)
    if quality_errors:
        raise RuntimeError(f"Arabic diacritization quality guard failed: {quality_errors}")
    if float(SETTINGS.get("tts_generation_speed", 0)) != 1.0:
        raise RuntimeError("TTS must generate at its normal speed 1.0")
    if float(SETTINGS.get("post_tts_tempo", 0)) != 1.0:
        raise RuntimeError("Post-processing tempo must remain at native speed 1.0")
    require_pausal_sentence_endings(api_tts_text)
    sentence_boundaries=len(re.findall(r"[.!؟]\n\n", api_tts_text))
    if sentence_boundaries < 5:
        raise RuntimeError("TTS text does not contain enough sentence pause boundaries")
    ad,vd=duration(story/"12_audio_full.mp3"),duration(story/"14_video_final.mp4")
    if abs(ad-vd)>2.5: raise RuntimeError(f"duration mismatch {ad} {vd}")
    video_path = story / "14_video_final.mp4"
    if video_path.stat().st_size <= 1_000_000:
        raise RuntimeError("VIDEO_QC_FAILED: final video is implausibly small")
    probe = subprocess.run([
        ffprobe(), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(video_path)
    ], capture_output=True, text=True, check=True)
    probe_data = json.loads(probe.stdout)
    streams = probe_data.get("streams") or []
    video_stream = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video_stream or not audio_stream:
        raise RuntimeError("VIDEO_QC_FAILED: missing audio or video stream")
    if (video_stream.get("width"), video_stream.get("height")) != (1920, 1080):
        raise RuntimeError("VIDEO_QC_FAILED: resolution is not 1920x1080")
    if video_stream.get("codec_name") != "h264" or audio_stream.get("codec_name") != "aac":
        raise RuntimeError("VIDEO_QC_FAILED: expected H.264 video and AAC audio")
    decode = subprocess.run(
        [ffmpeg(), "-v", "error", "-i", str(video_path), "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if decode.returncode:
        raise RuntimeError("VIDEO_QC_FAILED: full decode reported corruption")
    logs=story/"logs"; logs.mkdir(exist_ok=True)
    silence=subprocess.run([ffmpeg(),"-hide_banner","-i",str(story/"12_audio_full.mp3"),"-af","silencedetect=noise=-45dB:d=8","-f","null","-"],capture_output=True,text=True)
    long_silences=re.findall(r"silence_duration:\s*([0-9.]+)",silence.stderr)
    stats=subprocess.run([ffmpeg(),"-hide_banner","-i",str(story/"12_audio_full.mp3"),"-af","astats=metadata=1:reset=1","-f","null","-"],capture_output=True,text=True)
    duration_status="PASS" if SETTINGS["target_min_seconds"] <= ad <= SETTINGS["target_max_seconds"] else "FAIL"
    validation={"audio_duration_seconds":round(ad,2),"video_duration_seconds":round(vd,2),"duration_status":duration_status,"youtube_packaging_status":"PASS" if packaged_episode else "LEGACY","tts_normalization_status":"PASS","speech_safe_validation_status":"PASS","tts_diacritization_status":"PASS_PRONUNCIATION_ACCURATE","tts_final_diacritics_status":"PASS_PAUSAL","pronunciation_validation_status":"PASS","tts_preparation_status":"PASS","diacritic_ratio":round(current_diacritic_ratio,4),"diacritization_engine":DIACRITIZATION_VERSION,"tts_generation_speed":float(SETTINGS.get("tts_generation_speed",1.0)),"post_processing_tempo":float(SETTINGS.get("post_tts_tempo",1.0)),"effective_speed":float(SETTINGS.get("post_tts_tempo",1.0)),"sentence_pause_strategy":SETTINGS.get("sentence_pause_strategy"),"sentence_pause_boundaries":sentence_boundaries,"pausal_sentence_endings_status":"PASS","lexical_corrections_count":len(lexical_corrections),"lexical_corrections_preview":lexical_corrections[:20],"performance_tags":0,"long_silences_seconds":[float(x) for x in long_silences],"audio_chunks":len(list((story/"audio_chunks").glob("*.mp3"))),"tts_chunks":len(list((story/"tts_chunks").glob("*.txt"))),"zero_byte_files":[str(p) for p in story.rglob("*") if p.is_file() and p.stat().st_size==0]}
    (logs/"validation.json").write_text(json.dumps(validation,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (logs/"ffmpeg_astats.txt").write_text(stats.stderr,encoding="utf-8")
    if validation["zero_byte_files"]: raise RuntimeError("zero-byte files found")
    if validation["long_silences_seconds"]: raise RuntimeError("unnatural silence >= 8 seconds detected")
    if duration_status != "PASS": raise RuntimeError("audio duration is outside the approved range")
    chunk_models_path = logs / "tts_chunk_models.json"
    chunk_models = json.loads(chunk_models_path.read_text(encoding="utf-8")) if chunk_models_path.exists() else {}
    models_used = sorted(set(chunk_models.values()))
    from state_machine import State, transition_state
    meta=json.loads((story/"15_metadata.json").read_text(encoding="utf-8")); meta.update(audio_duration_seconds=round(ad,2),video_duration_seconds=round(vd,2),word_count=arabic_words((story/"05_story_clean.md").read_text(encoding="utf-8")),**voice_metadata(),tts_models_used=models_used,tts_normalization_status="passed",speech_safe_validation_status="passed",audio_generation_status="passed",audio_qc_status="passed",video_assembly_status="passed",video_qc_status="passed",metadata_validation_status="passed",clean_story_sha256=file_sha256(story/"05_story_clean.md"),tts_normalized_sha256=file_sha256(story/"05b_story_tts_normalized.md"),tts_api_sha256=file_sha256(story/"07_story_tts_api.md"),audio_sha256=file_sha256(story/"12_audio_full.mp3"),image_sha256=file_sha256(story/"10_background.png"),thumbnail_sha256=file_sha256(story/"11_thumbnail_base.jpg"),video_sha256=file_sha256(video_path),background_audio="pink noise",background_audio_volume_percent=background_volume_percent(),narration_audio_path=str(story/"12_audio_narration.mp3"),audio_path=str(story/"12_audio_full.mp3"),video_path=str(story/"14_video_final.mp4"),thumbnail_path=str(story/"11_thumbnail_base.jpg"),approval_status=State.MEDIA_READY,publish_status="not_published")
    (story/"15_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (story/"17_status.md").write_text(
        f"# الحالة\n\n- الحالة الحالية: `{State.MEDIA_READY}`\n- النشر: `not_published`\n- بانتظار مراجعة Marwa واعتماد صريح قبل النشر.\n",
        encoding="utf-8",
    )
    history_path=ROOT/"config"/"topics_history.json"
    history=json.loads(history_path.read_text(encoding="utf-8"))
    for topic in history:
        if topic.get("slug") == meta.get("slug"):
            current = topic.get("status", "")
            topic["status"] = transition_state(current, State.MEDIA_READY) if current else State.MEDIA_READY
            topic["story_path"]=story.relative_to(ROOT).as_posix()
    history_path.write_text(json.dumps(history,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"metadata":meta,"validation":validation},ensure_ascii=False,indent=2))

def package(story: Path):
    run_summary.stage("package")
    from state_machine import State
    meta=json.loads((story/"15_metadata.json").read_text(encoding="utf-8"))
    meta.update(audio_duration_seconds=round(duration(story/"12_audio_full.mp3"),2),video_duration_seconds=round(duration(story/"14_video_final.mp4"),2),word_count=arabic_words((story/"05_story_clean.md").read_text(encoding="utf-8")),**voice_metadata(),background_audio="pink noise",background_audio_volume_percent=background_volume_percent(),narration_audio_path=str(story/"12_audio_narration.mp3"),audio_path=str(story/"12_audio_full.mp3"),video_path=str(story/"14_video_final.mp4"),thumbnail_path=str(story/"11_thumbnail_base.jpg"),approval_status=State.MEDIA_READY,publish_status="not_published")
    (story/"15_metadata.json").write_text(json.dumps(meta,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    (story/"17_status.md").write_text(
        f"# الحالة\n\n- الحالة الحالية: `{State.MEDIA_READY}`\n- النشر: `not_published`\n- بانتظار مراجعة Marwa واعتماد صريح قبل النشر.\n",
        encoding="utf-8",
    )
    def md(name): return html.escape((story/name).read_text(encoding="utf-8"))
    page=f'''<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><title>{html.escape(meta['title'])}</title><style>body{{font-family:Segoe UI,Tahoma,sans-serif;background:#f4efe5;color:#4f4035;max-width:1050px;margin:auto;padding:32px;line-height:1.9}}img{{width:100%;border-radius:18px}}.card{{background:#fffaf0;padding:24px;margin:18px 0;border:1px solid #cec7b6;border-radius:16px}}pre{{white-space:pre-wrap;font:inherit}}a{{color:#826d5c}}</style><h1>{html.escape(meta['title'])}</h1><img src="10_background.png"><div class="card"><b>الموضوع:</b> {html.escape(meta['topic'])}<br><b>الكلمات:</b> {meta.get('word_count',0)}<br><b>مدة الصوت:</b> {meta.get('audio_duration_seconds',0)} ثانية<br><b>الخلفية الصوتية:</b> Pink Noise بنسبة {background_volume_percent():g}%<br><b>الحالة:</b> pending_approval<br><a href="12_audio_narration.mp3">صوت الراوي فقط</a> · <a href="12_audio_full.mp3">الصوت المخلوط</a> · <a href="13_audio_review.opus">نسخة المراجعة</a> · <a href="14_video_final.mp4">الفيديو</a> · <a href="11_thumbnail_base.jpg">Thumbnail</a></div><h2>النص</h2><div class="card"><pre>{md('05_story_clean.md')}</pre></div><h2>وصف YouTube</h2><div class="card"><pre>{md('02_description.txt')}</pre></div><h2>المصادر</h2><div class="card"><pre>{md('03_sources.md')}</pre></div></html>'''
    (story/"00_package.html").write_text(page,encoding="utf-8")


def completed_artifacts_are_current(story: Path) -> bool:
    metadata_path = story / "15_metadata.json"
    if not metadata_path.exists():
        return False
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    required_statuses = (
        "fact_check_status", "second_fact_check_status", "diacritization_status",
        "tts_normalization_status", "speech_safe_validation_status",
        "pronunciation_validation_status", "tts_preparation_status", "audio_qc_status",
        "image_generation_status", "image_accuracy_status", "video_qc_status",
        "metadata_validation_status",
    )
    if any(metadata.get(name) != "passed" for name in required_statuses):
        return False
    if metadata.get("youtube_packaging_status"):
        if metadata.get("youtube_packaging_validation_status") != "passed" or metadata.get("thumbnail_text_render_status") != "passed":
            return False

    # The whole-pipeline fast path must not accept a thumbnail whose Canva inputs
    # changed after the image stage last ran. File hashes alone only prove the
    # existing thumbnail is intact, not that it matches the current hook/prompt.
    thumbnail_text = str(metadata.get("thumbnail_text", "")).strip()
    if thumbnail_text:
        import canva_thumbnail

        scene_path = story / "08_image_prompt_en.txt"
        scene_prompt = scene_path.read_text(encoding="utf-8").strip() if scene_path.exists() else ""
        try:
            effective_thumbnail_prompt = canva_thumbnail.build_background_prompt(
                str(metadata.get("thumbnail_prompt", "")).strip(),
                scene_prompt,
            )
        except canva_thumbnail.CanvaError:
            return False
        if metadata.get("thumbnail_input_sha256") != canva_thumbnail.input_identity(
            thumbnail_text,
            effective_thumbnail_prompt,
        ):
            return False

    expected = {
        "clean_story_sha256": story / "05_story_clean.md",
        "tts_normalized_sha256": story / "05b_story_tts_normalized.md",
        "tts_api_sha256": story / "07_story_tts_api.md",
        "audio_sha256": story / "12_audio_full.mp3",
        "image_sha256": story / "10_background.png",
        "thumbnail_sha256": story / "11_thumbnail_base.jpg",
        "video_sha256": story / "14_video_final.mp4",
    }
    return all(path.exists() and metadata.get(key) == file_sha256(path) for key, path in expected.items())

def main():
    try:
        _main()
    except BaseException as exc:
        if not isinstance(exc, SystemExit):
            run_summary.fail(exc)  # the real root error, before the parent only sees "Command failed"
        raise

def _main():
    p=argparse.ArgumentParser(); p.add_argument("command",choices=["prepare","tts","image","render","validate","package","all"]); p.add_argument("story")
    a=p.parse_args(); story=Path(a.story).resolve()
    if a.command == "all" and completed_artifacts_are_current(story):
        validate(story)
        package(story)
        return
    if a.command in ("prepare","all"): prepare(story)
    if a.command in ("tts","all"): asyncio.run(synthesize(story))
    if a.command in ("image","all"): background(story)
    if a.command in ("render","all"): render(story)
    if a.command in ("validate","all"): validate(story)
    if a.command in ("package","all"): package(story)

if __name__ == "__main__": main()

