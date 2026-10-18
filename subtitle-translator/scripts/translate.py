import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pysubs2
import requests


OLLAMA_URL = os.getenv("OLLAMA_URL", "http://ollama:11434").rstrip("/")
MODEL = os.getenv("OLLAMA_MODEL", "translate-gemma-sub-e4b")

# An Translate Gemma 4 Sub angepasste Parameter
BATCH_SIZE = 5
CONTEXT_BEFORE = 15
CONTEXT_AFTER = 15
MAX_PREVIOUS_TRANSLATIONS = 20
NUM_CTX = 16384
REQUEST_TIMEOUT = 600
MAX_SUBTITLE_CHARS = 40
TEMPERATURE = 0.3
NUM_PREDICT = 2048

# Synopsis-Kürzung: 1200 Zeichen reichen für Prämisse + Hauptfiguren
MAX_SYNOPSIS_CHARS = 1200

# Anzahl AniList-Treffer, die beim interaktiven Blättern angeboten werden
ANILIST_SEARCH_RESULTS = 8

# Strenger Modus: bei fehlgeschlagenen Übersetzungen mit Exit-Code 1 enden
STRICT_TRANSLATION = os.getenv("STRICT_TRANSLATION", "0") == "1"

# Verzeichnisnamen, die als Bibliotheksgrenze gelten – oberhalb wird nicht gesucht
LIBRARY_ROOTS = {"serien", "filme", "movies", "series", "anime", "tv"}

# Muster, die ein Verzeichnis als Season/OVA/Episode-Marker erkennen lassen
NON_TITLE_DIR_PATTERNS = [
    re.compile(r"^(?:staffel|season|s)\s*\d+$", re.IGNORECASE),
    re.compile(r"^s\d{1,2}$", re.IGNORECASE),
    re.compile(r"^\d+$"),
    re.compile(r"^(?:ova|oad|special|specials|extra|extras)$", re.IGNORECASE),
    re.compile(r"^(?:episode|ep|folge)\s*\d+$", re.IGNORECASE),
    re.compile(r"^(?:part|cour)\s*\d+$", re.IGNORECASE),
    re.compile(r"^(?:disc|cd|dvd|bd)\s*\d+$", re.IGNORECASE),
]

TEXT_CODECS = {
    "S_TEXT/UTF8": ".srt",
    "S_TEXT/ASS": ".ass",
    "S_TEXT/SSA": ".ssa",
    "S_TEXT/WEBVTT": ".vtt",
    "SubStationAlpha": ".ssa",
    "SubRip/SRT": ".srt",
    "SubRip": ".srt",
    "SSA": ".ssa",
    "ASS": ".ass",
}

# FIX 4A: Bild-Codecs für mkvmerge-Pfad an ffmpeg-Pfad angeglichen.
IMAGE_CODECS_MKV = {
    "S_HDMV/PGS": "PGS",
    "S_VOBSUB": "VobSub",
    "S_DVBSUB": "DVB",
}

SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".ts", ".m2ts", ".mts"}

# Container, die mkvmerge/mkvextract lesen können.
MKV_FAMILY_EXTENSIONS = {".mkv", ".webm", ".ts", ".m2ts", ".mts"}

# ffprobe codec_name -> (unsere Codec-Bezeichnung, Datei-Endung)
FFMPEG_TEXT_SUBTITLE_CODECS = {
    "subrip": ("SubRip", ".srt"),
    "srt": ("SubRip", ".srt"),
    "ass": ("ASS", ".ass"),
    "ssa": ("SSA", ".ssa"),
    "webvtt": ("WEBVTT", ".vtt"),
    "mov_text": ("mov_text", ".srt"),
    "text": ("text", ".srt"),
}

FFMPEG_IMAGE_SUBTITLE_CODECS = {
    "hdmv_pgs_subtitle": "PGS",
    "dvd_subtitle": "VobSub",
    "dvb_subtitle": "DVB",
    "xsub": "XSUB",
}

KNOWN_STYLE_NAMES = {
    "Default", "default", "Standard", "Main", "Dialogue", "Sign", "Signs",
    "Marulks", "Alltag", "Top", "Bottom", "Italics", "Bold", "Note",
    "OP", "ED", "Opening", "Ending", "Title", "Song", "Karaoke",
    "Romaji", "Kanji", "English", "German", "Deutsch", "Englisch",
    "Alternate", "Alt", "Comment", "Notes",
}

ALLOWED_SHORT_WORDS = {
    "Bitte", "Danke", "Hallo", "Tschüss", "Ja", "Nein", "Okay", "Ok",
    "Gut", "Super", "Klasse", "Hilfe", "Stopp", "Achtung", "Halt",
    "Mama", "Papa", "Meister", "Herr", "Frau", "Kind", "Master",
    "Was", "Wer", "Wie", "Wo", "Warum", "Wann", "Hier", "Da", "Dort",
    "Los", "Raus", "Rein", "Auf", "Ab", "Komm", "Geh", "Warte",
    "Stop", "Wait", "Run", "Go", "Hello", "Yes", "No", "What", "Why",
    "How", "Who", "Where", "When", "Here", "There", "Now", "Come",
}

# Interjektionen, bei denen eine unveränderte Übersetzung legitim ist
UNCHANGED_OK_TOKENS = {
    "oh", "ah", "eh", "uh", "hm", "hmm", "hmmm", "mhm", "mm", "mmm",
    "ha", "haha", "hah", "he", "hehe", "hi", "hihi", "ho", "hoho",
    "hey", "hi", "huh", "wow", "uh", "uhh", "ohh", "ahh", "ehh",
    "psst", "pst", "shh", "shhh", "tsk", "tch", "hmpf", "hmph",
    "ok", "okay", "yes", "no", "yeah", "yep", "nope", "nah",
    "stop", "wait", "run", "go", "help", "hello", "bye",
    "what", "why", "how", "who", "where", "when",
    "ja", "nein", "bitte", "danke", "hallo", "tschüss", "gut",
}

IDIOM_FIXES = {
    r"\bIch kann darauf anstoßen\b": "Darauf kann ich anstoßen",
    r"\bIch könnte darauf anstoßen\b": "Darauf könnte ich anstoßen",
    r"\bIch ziehe dir (nur )?das Bein\b": "Ich veralbere dich",
    r"\bDu ziehst mir (nur )?das Bein\b": "Du veralberst mich",
    r"\bDas ist ein Stück Kuchen\b": "Das ist ein Kinderspiel",
    r"\bLass uns die Straße schlagen\b": "Lass uns losfahren",
    r"\bBrich dir ein Bein\b": "Viel Glück",
    r"\bIch bin ganz Ohren\b": "Ich bin ganz Ohr",
    r"\bIch bin gebrochen\b": "Ich bin pleite",
}

# System-Prompt im von Translate Gemma 4 Sub erwarteten Format
SYSTEM_PROMPT_BASE = """TASK: Translate English subtitles into German.
SOURCE_LANGUAGE: English
TARGET_LANGUAGE: German
STYLE: Natural spoken German, suitable for anime dialogue. Preserve tone, humor, slang, and character voice. Keep names untranslated.

RULES:
1. Translate ONLY the target lines. Context lines are for understanding only.
2. Return exactly as many translations as target lines, in the same order.
3. NO line breaks in the translation. No \\n, no \\N, no <br>.
4. No quotation marks around translations, no explanations, no "Translation:" prefix.
5. Aim for max ~40 characters per line. If longer, rephrase – do not break lines.
6. Natural spoken German. No literal translation, no bureaucratic language.
7. Preserve humor, irony, profanity, subtext, and character voices.
8. Use consistent address ("du" or "Sie") per scene – never switch within a scene.
9. Do NOT translate proper names. Keep terminology consistent.
10. Do not invent or omit anything. Do not pull content from context.
11. Adapt idioms by meaning, not literally.
12. For ambiguous phrases, use neighboring line context.
13. Short exclamations (Oh!, Ah!, Huh?) should sound natural in German.

CONSISTENCY WITH PREVIOUS TRANSLATIONS:
- The [PREVIOUS_TRANSLATION] block shows how earlier lines were already translated.
- Reuse the same terms, titles, and address forms. Do NOT switch between "Meister" / "Meisterin" / "Herr" for the same character once established.
- Match the register (du/Sie) already used in the previous translations.

SPEAKER GENDER & TITLES:
- Determine the gender of the addressed character from context: pronouns (he/she), name, or how they appear in earlier lines.
- If a female character is called "Master", translate as "Meisterin" (never "Herr", never "Master").
- If a male character is called "Master": use "Meister" for a title, "Herr" for direct/formal address.
- If the gender cannot be determined from context, use the neutral "Meister" rather than guessing "Herr" or "Meisterin".

IDIOM EXAMPLES (translate by meaning, not literally):
- "I can toast to that" → "Darauf kann ich anstoßen" (NOT "Ich kann darauf anstoßen")
- "I'm just pulling your leg" → "Ich veralbere dich doch nur" (NOT "Ich ziehe dir nur das Bein")
- "You're pulling my leg" → "Du veralberst mich" (NOT "Du ziehst an meinem Bein")
- "It's a piece of cake" → "Das ist ein Kinderspiel" (NOT "Das ist ein Stück Kuchen")
- "Let's hit the road" → "Lass uns losfahren" (NOT "Lass uns die Straße schlagen")
- "Break a leg" → "Viel Glück" (NOT "Brich ein Bein")
- "I'm all ears" → "Ich bin ganz Ohr" (wörtlich okay, aber Plural vermeiden)
- "Speak of the devil" → "Wenn man vom Teufel spricht" (wörtlich okay)
- "Better late than never" → "Besser spät als nie" (wörtlich okay)
- "I'm broke" → "Ich bin pleite" (NOT "Ich bin gebrochen")

Note: some idioms work literally in German (see last three examples). Do NOT rephrase those – only rephrase when the literal version sounds unnatural in German.

Respond ONLY with valid JSON in exactly this format:
{"translations": ["German line 1", "German line 2", ...]}"""


def apply_idiom_fixes(text: str) -> str:
    for pattern, replacement in IDIOM_FIXES.items():
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    return text


def run_command(args):
    return subprocess.run(
        args, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    ).stdout


def inspect_video(path):
    return json.loads(run_command(["mkvmerge", "-J", str(path)]))


def find_english_text_track(info):
    tracks = [t for t in info.get("tracks", []) if t.get("type") == "subtitles"]
    if not tracks:
        # FIX 3: OCR-Meldung präzisiert
        raise RuntimeError(
            "Keine Untertiteltracks gefunden. Das Skript führt keine OCR "
            "durch und kann keine eingebrannten Untertitel extrahieren. "
            "Bitte eine separate .srt/.ass/.vtt-Datei übergeben oder "
            "Untertitel extern per OCR erzeugen."
        )

    print("\nUntertiteltracks:")
    for track in tracks:
        props = track.get("properties", {})
        print(
            f"ID={track['id']} | "
            f"Sprache={props.get('language', 'und')} | "
            f"Codec={track.get('codec', '?')} | "
            f"Name={props.get('track_name', '')}"
        )

    english = [
        t for t in tracks
        if (
            t.get("properties", {}).get("language", "").lower() in {"en", "eng"}
            or t.get("properties", {}).get("language_ietf", "").lower() == "en"
            or t.get("properties", {}).get("language_ietf", "").lower().startswith("en-")
        )
    ]

    text_tracks = [t for t in english if t.get("codec") in TEXT_CODECS]
    if text_tracks:
        return next(
            (t for t in text_tracks if t.get("properties", {}).get("default_track", False)),
            text_tracks[0],
        )

    # FIX 4A: Bilduntertitel über IMAGE_CODECS_MKV erkennen und Codec-Namen nennen
    image_tracks = [
        t for t in english if t.get("codec") in IMAGE_CODECS_MKV
    ]
    if image_tracks:
        names = ", ".join(
            IMAGE_CODECS_MKV[t["codec"]] for t in image_tracks
        )
        raise RuntimeError(
            f"Englische Bilduntertitel gefunden ({names}). "
            "Diese müssen zuerst per OCR in Text umgewandelt werden "
            "(z. B. Subtitle Edit, SubExtractor, VobSub2SRT) und dann "
            "als .srt/.ass/.vtt übergeben werden."
        )

    raise RuntimeError("Kein englischer Textuntertitel gefunden.")


def extract_track(video, track):
    codec = track.get("codec")
    extension = TEXT_CODECS.get(codec)
    if not extension:
        raise RuntimeError(f"Nicht unterstützter Codec: {codec}")

    with tempfile.TemporaryDirectory() as temp_dir:
        subtitle_path = Path(temp_dir) / f"source{extension}"
        run_command(["mkvextract", "tracks", str(video), f"{track['id']}:{subtitle_path}"])
        return subtitle_path.read_text(encoding="utf-8-sig"), extension


# ---------------------------------------------------------------------------
# ffprobe/ffmpeg-Fallback für MP4, AVI, MOV usw.
# ---------------------------------------------------------------------------

def _ffprobe_subtitle_streams(path: Path) -> list[dict]:
    """Liest Untertitel-Streams via ffprobe. Gibt [] zurück, wenn keine da sind."""
    raw = run_command([
        "ffprobe", "-v", "error",
        "-select_streams", "s",
        "-show_entries",
        "stream=index,codec_name:stream_tags=language,title:stream_disposition=default",
        "-of", "json",
        str(path),
    ])
    data = json.loads(raw) if raw.strip() else {}
    return data.get("streams", []) or []


def _ffmpeg_muxer_and_codec_for_extension(extension: str) -> tuple[str, str]:
    """Liefert (Muxer, Encoder) für die Zielendung."""
    mapping = {
        ".srt": ("srt", "srt"),
        ".ass": ("ass", "ass"),
        ".ssa": ("ass", "ass"),
        ".vtt": ("webvtt", "webvtt"),
    }
    return mapping.get(extension, ("srt", "srt"))


def _ffmpeg_extract_subtitle_stream(
    path: Path, stream_index: int, extension: str
) -> tuple[str, str]:
    muxer, encoder = _ffmpeg_muxer_and_codec_for_extension(extension)
    with tempfile.TemporaryDirectory() as temp_dir:
        out_path = Path(temp_dir) / f"source{extension}"
        run_command([
            "ffmpeg", "-v", "error", "-y",
            "-i", str(path),
            "-map", f"0:{stream_index}",
            "-c:s", encoder,
            "-f", muxer,
            str(out_path),
        ])
        return out_path.read_text(encoding="utf-8-sig"), extension


def load_subtitle_source_via_ffmpeg(input_path: Path):
    print(f"  mkvmerge kann {input_path.suffix} nicht lesen – versuche ffprobe/ffmpeg …")
    streams = _ffprobe_subtitle_streams(input_path)
    if not streams:
        # FIX 3: OCR-Meldung präzisiert
        raise RuntimeError(
            "Keine Untertitelspuren in der Datei gefunden. "
            "Das Skript kann nur vorhandene Textuntertitel übersetzen "
            "und führt selbst keine OCR durch. Bei eingebrannten "
            "Untertiteln müssen diese zunächst extern per OCR extrahiert "
            "werden (z. B. Subtitle Edit, SubExtractor). Alternativ eine "
            "separate .srt-, .ass- oder .vtt-Datei übergeben."
        )

    print("\nUntertiteltracks (ffprobe):")
    for s in streams:
        tags = s.get("tags", {}) or {}
        disp = s.get("disposition", {}) or {}
        print(
            f"Stream #{s['index']} | "
            f"Sprache={tags.get('language', 'und')} | "
            f"Codec={s.get('codec_name', '?')} | "
            f"Name={tags.get('title', '')} | "
            f"Default={'ja' if disp.get('default') else 'nein'}"
        )

    english = []
    for s in streams:
        lang = (s.get("tags", {}) or {}).get("language", "").lower()
        if lang in {"en", "eng"} or lang.startswith("en-"):
            english.append(s)

    if not english:
        untagged = [
            s for s in streams
            if not (s.get("tags", {}) or {}).get("language")
        ]
        if untagged:
            print(
                "  Kein Stream explizit 'en' markiert – "
                f"nutze Stream(s) ohne Sprach-Tag ({len(untagged)} gefunden)."
            )
            english = untagged

    if not english:
        image_streams = [
            s for s in streams
            if (s.get("codec_name") or "").lower() in FFMPEG_IMAGE_SUBTITLE_CODECS
        ]
        if image_streams:
            names = ", ".join(
                FFMPEG_IMAGE_SUBTITLE_CODECS[(s["codec_name"] or "").lower()]
                for s in image_streams
            )
            raise RuntimeError(
                f"Bilduntertitel gefunden ({names}), aber keine englischen "
                "Textspuren. Für die Übersetzung wird eine englische "
                "Textspur benötigt. Bitte zuerst per OCR (z. B. Subtitle Edit, "
                "SubExtractor, VobSub2SRT) in eine .srt/.ass/.vtt-Datei "
                "umwandeln und diese dann dem Skript übergeben."
            )
        raise RuntimeError(
            "Keine englischen Untertitelspuren gefunden. "
            f"Gefundene Sprachen: "
            f"{sorted({(s.get('tags') or {}).get('language') or 'und' for s in streams})}."
        )

    text_streams = [
        s for s in english
        if (s.get("codec_name") or "").lower() in FFMPEG_TEXT_SUBTITLE_CODECS
    ]
    if text_streams:
        default_text = next(
            (s for s in text_streams
             if (s.get("disposition", {}) or {}).get("default")),
            text_streams[0],
        )
        codec_name = (default_text.get("codec_name") or "").lower()
        _, extension = FFMPEG_TEXT_SUBTITLE_CODECS[codec_name]
        print(
            f"\nVerwende Stream #{default_text['index']} "
            f"({codec_name} → {extension})."
        )
        raw, ext = _ffmpeg_extract_subtitle_stream(
            input_path, default_text["index"], extension
        )
        return pysubs2.SSAFile.from_string(raw), ext

    image_streams = [
        s for s in english
        if (s.get("codec_name") or "").lower() in FFMPEG_IMAGE_SUBTITLE_CODECS
    ]
    if image_streams:
        names = ", ".join(
            FFMPEG_IMAGE_SUBTITLE_CODECS[(s["codec_name"] or "").lower()]
            for s in image_streams
        )
        raise RuntimeError(
            f"Englische Bilduntertitel gefunden ({names}). "
            "Diese sind keine Textspuren und können nicht direkt übersetzt "
            "werden. Bitte zuerst per OCR (z. B. Subtitle Edit, "
            "SubExtractor, VobSub2SRT) in eine .srt/.ass/.vtt-Datei "
            "umwandeln und diese dann dem Skript übergeben."
        )

    raise RuntimeError(
        "Englische Untertitelspuren vorhanden, aber kein unterstütztes "
        "Textformat (subrip/ass/ssa/webvtt/mov_text). "
        f"Gefundene Codecs: {sorted({s.get('codec_name') for s in english})}. "
        "Bitte Untertitel extern konvertieren oder per OCR erzeugen."
    )


# ---------------------------------------------------------------------------
# Text-/Event-Bereinigung
# ---------------------------------------------------------------------------

def is_drawing_path(text: str) -> bool:
    stripped = text.strip()
    if not stripped or not re.match(r"^[mM]\s", stripped):
        return False
    residue = re.sub(r"[mMlLbBcCsSpPqQtTaA\d\s\.\,\-]", "", stripped)
    return residue == ""


def is_probable_style_name(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if stripped in KNOWN_STYLE_NAMES:
        return True
    if stripped in ALLOWED_SHORT_WORDS:
        return False
    if not re.fullmatch(r"[A-ZÄÖÜ][A-Za-zÄÖÜäöüß\-]+", stripped):
        return False
    return len(stripped) <= 10


def clean_source_text(text: str, is_ass: bool = False) -> str:
    if is_ass:
        text = re.sub(r"\{[^}]*\}", "", text)
        text = re.sub(r"\\[Nn]", " ", text)
        text = re.sub(r"\\[kK][fFoO]?\d+", " ", text)
        text = re.sub(
            r"\b[mM]\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?"
            r"(?:\s+[lLbBcCsSpPqQtTaA]\s+-?\d+(?:\.\d+)?\s+-?\d+(?:\.\d+)?)+",
            " ", text,
        )

    text = html.unescape(text)
    text = re.sub(r"<\s*br\s*/?\s*>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def event_is_valid(event, is_ass: bool = False) -> bool:
    if not event.text.strip():
        return False

    if getattr(event, "is_drawing", False):
        return False
    if getattr(event, "is_comment", False):
        return False
    if getattr(event, "is_picture", False):
        return False

    event_type = getattr(event, "type", None)
    if event_type in {"Drawing", "Comment", "Picture"}:
        return False

    if is_ass and is_probable_style_name(event.text):
        return False

    cleaned = clean_source_text(event.text, is_ass=is_ass)
    if not cleaned:
        return False

    # ASS-Zeichenpfade nach der Bereinigung ausschließen.
    if is_ass and is_drawing_path(cleaned):
        return False

    # Kurze Großbuchstaben nicht pauschal entfernen:
    # "YOU", "RUN", "WHY" usw. können echte Dialoge sein.
    return True


def dedupe_events(events):
    seen = set()
    result = []
    for event in events:
        key = (event.start, event.end, event.text.strip())
        if key in seen:
            continue
        seen.add(key)
        result.append(event)
    return result


def wrap_subtitle(
    text: str,
    max_chars: int = MAX_SUBTITLE_CHARS,
    line_sep: str = r"\N",
) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text

    words = text.split()
    lines: list[str] = []
    current = ""

    for word in words:
        candidate = f"{current} {word}".strip()
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word

    if current:
        lines.append(current)

    if len(lines) <= 2:
        return line_sep.join(lines)

    for budget in range(max_chars - 2, max(10, max_chars // 2), -2):
        lines = []
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if len(candidate) <= budget:
                current = candidate
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
        if len(lines) <= 2:
            return line_sep.join(lines)

    half = len(text) // 2
    split_at = text.rfind(" ", 0, half + 10)
    if split_at <= 0:
        split_at = half
    return text[:split_at].strip() + line_sep + text[split_at:].strip()


def _is_unchanged_allowed(text: str) -> bool:
    stripped = text.strip().lower()
    if not stripped:
        return False
    tokens = re.findall(r"[a-zäöüß]+", stripped)
    if not tokens:
        return True
    return all(tok in UNCHANGED_OK_TOKENS for tok in tokens)


def _looks_like_translatable(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if re.fullmatch(r"[A-ZÄÖÜ]-[A-ZÄÖÜ][a-zäöüß]+[!?.…]*", stripped):
        return False

    words = stripped.split()
    if len(words) >= 3:
        return True

    lower_words = [w for w in words if re.search(r"[a-zäöüß]", w)]
    if len(lower_words) >= 2:
        return True

    if lower_words:
        w = lower_words[0].lower().strip(".,!?…")
        if w in {
            "der", "die", "das", "ein", "eine", "und", "oder", "aber",
            "ich", "du", "er", "sie", "es", "wir", "ihr", "was", "wie",
            "wo", "wer", "warum", "wann", "bitte", "danke", "ja", "nein",
            "nicht", "kein", "keine", "ist", "sind", "war", "waren",
            "hat", "haben", "hatte", "hatten", "wird", "werden",
            "kann", "können", "muss", "müssen", "will", "wollen",
            "soll", "sollen", "darf", "dürfen", "mag", "mögen",
            "hier", "dort", "da", "dann", "jetzt", "noch", "schon",
            "immer", "nie", "nur", "auch", "sehr", "so", "zu",
            "auf", "in", "an", "mit", "für", "von", "bei", "nach",
            "vor", "über", "unter", "durch", "gegen", "ohne", "um",
        }:
            return True

    return len(stripped) > 6


def _format_context_block(name: str, items: list[str]) -> str:
    if not items:
        return f"[{name}]\n(none)\n"
    return f"[{name}]\n" + "\n".join(items) + "\n"


# ---------------------------------------------------------------------------
# AniList / interaktive Titelwahl
# ---------------------------------------------------------------------------

def fetch_anilist_search(search_title: str, per_page: int = ANILIST_SEARCH_RESULTS) -> list[dict]:
    query = """
    query ($search: String!, $perPage: Int!) {
      Page(perPage: $perPage) {
        media(search: $search, type: ANIME) {
          id
          title { romaji english native }
          description(asHtml: false)
          genres
          seasonYear
          format
          studios(isMain: true) { nodes { name } }
        }
      }
    }
    """
    try:
        response = requests.post(
            "https://graphql.anilist.co",
            json={"query": query, "variables": {"search": search_title, "perPage": per_page}},
            timeout=15,
        )
        response.raise_for_status()
        data = response.json()
        return data.get("data", {}).get("Page", {}).get("media", []) or []
    except (requests.RequestException, ValueError, KeyError):
        return []


def fetch_anilist_metadata(search_title: str) -> dict | None:
    results = fetch_anilist_search(search_title, per_page=1)
    return results[0] if results else None


def _format_anilist_hit(meta: dict, index: int | None = None) -> str:
    title_obj = meta.get("title", {}) or {}
    romaji = title_obj.get("romaji") or title_obj.get("english") or "?"
    english = title_obj.get("english")
    year = meta.get("seasonYear")
    fmt = meta.get("format")
    genres = meta.get("genres", []) or []
    studios = meta.get("studios", {}).get("nodes", []) or []
    studio_names = ", ".join(s.get("name", "?") for s in studios if s.get("name"))

    prefix = f"[{index}] " if index is not None else ""
    line = f"{prefix}{romaji}"
    details = []
    if english and english != romaji:
        details.append(f"EN: {english}")
    if year:
        details.append(str(year))
    if fmt:
        details.append(fmt)
    if genres:
        details.append("/".join(genres[:3]))
    if studio_names:
        details.append(studio_names)
    if details:
        line += "  –  " + " | ".join(details)
    return line


def _prompt_title_decision(
    hits: list[dict],
    search_term: str,
) -> tuple[dict | None, str | None]:
    """
    Interaktiver Dialog für eine Trefferliste.
    Rückgabe: (gewählter Treffer oder None, neuer Suchbegriff oder None)

    Optionen:
      j / Enter = aktuellen Treffer bestätigen
      n         = nächster Treffer
      p         = vorheriger Treffer
      m         = neuen Suchbegriff eingeben
      s         = komplett überspringen (ohne AniList-Kontext weiter)
      1-9       = direkt zu diesem Index springen
    """
    if not hits:
        return None, None

    if not sys.stdin.isatty():
        print("  (Kein interaktives Terminal – übernehme ersten Treffer automatisch.)")
        return hits[0], None

    index = 0
    while True:
        hit = hits[index]
        print()
        print(f"  Suchbegriff: {search_term!r}")
        print(f"  Treffer {index + 1}/{len(hits)}:")
        print("    " + _format_anilist_hit(hit))

        prompt = (
            "  [j]a übernehmen  [n]ächster  [p]vorheriger  "
            "[m]anuell suchen  [s]kip  [1-9] direkt: "
        )
        try:
            answer = input(prompt).strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return hit, None

        if answer in {"j", "ja", "y", "yes", ""}:
            return hit, None
        if answer == "n":
            if index + 1 < len(hits):
                index += 1
            else:
                print("  Keine weiteren Treffer. [m]anuell suchen oder [s]kip.")
        elif answer == "p":
            if index > 0:
                index -= 1
            else:
                print("  Schon beim ersten Treffer.")
        elif answer == "m":
            try:
                new_term = input("  Neuer Suchbegriff (leer = abbrechen): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return hit, None
            if new_term:
                return None, new_term
        elif answer == "s":
            return None, None
        elif answer.isdigit() and 1 <= int(answer) <= len(hits):
            index = int(answer) - 1
        else:
            print("  Ungültige Eingabe.")


def _looks_like_non_title_dir(name: str) -> bool:
    stripped = name.strip()
    if not stripped:
        return True
    for pattern in NON_TITLE_DIR_PATTERNS:
        if pattern.match(stripped):
            return True
    return False


def _candidate_titles_from_path(input_path: Path) -> list[str]:
    current = input_path.parent
    collected: list[str] = []
    found_library_root = False

    while current and current.name:
        if current.name.lower() in LIBRARY_ROOTS:
            found_library_root = True
            break
        collected.append(current.name)
        parent = current.parent
        if parent == current:
            break
        current = parent

    if not found_library_root or not collected:
        return []

    authoritative = collected[-1]
    fallbacks = [c for c in collected[:-1] if not _looks_like_non_title_dir(c)]

    ordered: list[str] = []
    for name in [authoritative] + fallbacks:
        cleaned = clean_source_text(name)
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def _search_loop(
    initial_term: str,
    default_fallback: str | None,
) -> tuple[str | None, dict | None]:
    """
    FIX 2 + FIX 3: Gemeinsame Suchschleife, die mit einem vorhandenen Begriff
    startet und den eingegebenen Begriff direkt weiterverarbeitet, statt
    erneut nachzufragen. Der zurückgegebene Titel stammt aus dem
    ausgewählten AniList-Eintrag (romaji, Fallback english), damit
    Prompt-Titel und Synopsis konsistent sind.
    """
    current_term = initial_term
    while True:
        print(f"  Frage AniList ab für: {current_term!r}")
        hits = fetch_anilist_search(current_term)
        if not hits:
            print(f"  Kein Treffer für {current_term!r}.")
            if not sys.stdin.isatty():
                return default_fallback, None
            try:
                current_term = input(
                    "  Anderer Suchbegriff (Enter = ohne Synopsis fortfahren): "
                ).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return default_fallback, None
            if not current_term:
                return default_fallback, None
            continue

        chosen, new_term = _prompt_title_decision(hits, current_term)
        if chosen is not None:
            # FIX 3: Titel aus dem gewählten AniList-Eintrag ableiten
            title_obj = chosen.get("title", {}) or {}
            resolved = (
                title_obj.get("romaji")
                or title_obj.get("english")
                or current_term
            )
            print(f"  Bestätigt: {resolved!r}")
            return resolved, chosen
        if new_term:
            # FIX 2: neuen Begriff direkt weiterverwenden
            current_term = new_term
            continue

        print("  Auswahl übersprungen. Übersetze ohne AniList-Kontext.")
        return default_fallback, None


def _manual_anilist_lookup(
    default_fallback: str | None,
    initial_term: str | None = None,
) -> tuple[str | None, dict | None]:
    """
    Bietet manuelle Suche an. FIX 2: initial_term wird direkt verwendet,
    ohne erneut nachzufragen.
    """
    if not sys.stdin.isatty():
        print("  (Kein interaktives Terminal – keine manuelle Suche möglich.)")
        return default_fallback, None

    if initial_term:
        term = initial_term
    else:
        try:
            term = input(
                "\n  AniList-Suchtitel manuell eingeben "
                "(Enter = ohne Synopsis fortfahren): "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return default_fallback, None

    if not term:
        return default_fallback, None

    return _search_loop(term, default_fallback)


def resolve_anime_title(input_path: Path) -> tuple[str | None, dict | None]:
    candidates = _candidate_titles_from_path(input_path)

    if candidates:
        print(f"  Titel-Kandidaten (priorisiert): {candidates}")

        for candidate in candidates:
            print(f"  Frage AniList ab für: {candidate!r}")
            hits = fetch_anilist_search(candidate)
            if not hits:
                print(f"  Kein Treffer für {candidate!r}, versuche nächsten Kandidaten ...")
                continue

            chosen, new_term = _prompt_title_decision(hits, candidate)
            if chosen is not None:
                # FIX 4: AniList-Titel als kanonischen Titel verwenden,
                # damit Prompt-Titel und Synopsis konsistent sind.
                title_obj = chosen.get("title", {}) or {}
                resolved = (
                    title_obj.get("romaji")
                    or title_obj.get("english")
                    or candidate
                )
                print(f"  Bestätigt: {resolved!r}")
                return resolved, chosen
            if new_term:
                return _manual_anilist_lookup(
                    default_fallback=candidate,
                    initial_term=new_term,
                )

            print("  Auswahl übersprungen. Übersetze ohne AniList-Kontext.")
            return candidate, None

        print("  Kein AniList-Treffer für alle Pfad-Kandidaten.")
        return _manual_anilist_lookup(default_fallback=candidates[0])

    print("  Keine Titel-Kandidaten aus Verzeichnisstruktur gefunden.")
    return _manual_anilist_lookup(default_fallback=None)


def build_system_prompt(anime_title: str | None, anilist_meta: dict | None = None) -> str:
    prompt = SYSTEM_PROMPT_BASE
    if not anime_title and not anilist_meta:
        return prompt

    parts = ["\n\nSERIES CONTEXT:"]
    if anime_title:
        parts.append(f'- The source material is from the anime "{anime_title}".')
    if anilist_meta:
        desc = anilist_meta.get("description")
        if desc:
            # FIX 4: html.unescape VOR dem Tag-Strippen, damit Entities
            # wie &amp; korrekt zu & werden und nicht im Prompt verbleiben.
            clean_desc = html.unescape(re.sub(r"<[^>]+>", "", desc)).strip()
            clean_desc = re.sub(r"\s+", " ", clean_desc)
            parts.append(f"- Synopsis (English): {clean_desc[:MAX_SYNOPSIS_CHARS]}")
        genres = anilist_meta.get("genres", [])
        if genres:
            parts.append(f"- Genres: {', '.join(genres)}")
        studios = anilist_meta.get("studios", {}).get("nodes", [])
        studio_names = [s["name"] for s in studios if s.get("name")]
        if studio_names:
            parts.append(f"- Studio: {', '.join(studio_names)}")

    parts.append(
        "- This context is in English. Use it only to understand the story, "
        "characters, and relationships. Do NOT translate it."
    )
    parts.append("- Do NOT let this override the actual subtitle content.")
    return prompt + "\n".join(parts)


def translate_batch(
    target_texts,
    context_before,
    context_after,
    previous_source=None,
    previous_translation=None,
    system_prompt: str | None = None,
):
    previous_source = previous_source or []
    previous_translation = previous_translation or []
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT_BASE

    # FIX 5: previous_source und previous_translation gemeinsam kürzen,
    # damit sie aligned bleiben (gleiche Länge).
    max_len = min(
        len(previous_source),
        len(previous_translation),
        MAX_PREVIOUS_TRANSLATIONS,
    )
    if max_len > 0:
        previous_source = previous_source[-max_len:]
        previous_translation = previous_translation[-max_len:]
    else:
        previous_source = []
        previous_translation = []

    user_prompt = (
        _format_context_block("PREVIOUS_SOURCE", previous_source)
        + _format_context_block("PREVIOUS_TRANSLATION", previous_translation)
        + _format_context_block(
            "CONTEXT_BEFORE (source, for understanding only, DO NOT translate)",
            context_before,
        )
        + f"[CURRENT_SOURCE] (translate exactly {len(target_texts)} lines, same order)\n"
        + "[\n"
        + "\n".join(json.dumps(t, ensure_ascii=False) for t in target_texts)
        + "\n]\n\n"
        + _format_context_block(
            "CONTEXT_AFTER (source, for understanding only, DO NOT translate)",
            context_after,
        )
        + '\nRespond ONLY with valid JSON: {"translations": ["...", ...]}'
    )

    response = requests.post(
        f"{OLLAMA_URL}/api/chat",
        json={
            "model": MODEL,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "options": {
                "temperature": TEMPERATURE,
                "num_ctx": NUM_CTX,
                "num_predict": NUM_PREDICT,
                "top_p": 0.9,
            },
        },
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    content = response.json()["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.MULTILINE).strip()

    try:
        translations = json.loads(content)["translations"]
    except (json.JSONDecodeError, KeyError) as exc:
        raise ValueError(f"JSON-Antwort ungültig: {content[:200]!r}") from exc

    if not isinstance(translations, list):
        raise ValueError("translations ist keine Liste.")
    if len(translations) != len(target_texts):
        raise ValueError(f"Anzahl falsch: {len(translations)} statt {len(target_texts)}.")

    cleaned = []
    for i, (src, dst) in enumerate(zip(target_texts, translations)):
        if not isinstance(dst, str) or not dst.strip():
            raise ValueError(f"Zeile {i} leer: {dst!r}")

        dst = dst.strip()
        dst = re.sub(r"\\[Nn]|<\s*br\s*/?\s*>|\r?\n", " ", dst, flags=re.IGNORECASE)
        dst = re.sub(r"\s+", " ", dst).strip()
        dst = apply_idiom_fixes(dst)

        if (
            src.strip().lower() == dst.lower()
            and _looks_like_translatable(src)
            and not _is_unchanged_allowed(src)
        ):
            raise ValueError(f"Zeile {i} unverändert: {src!r}")

        cleaned.append(dst)

    return cleaned


def _translate_line_by_line(
    target_texts,
    context_before,
    context_after,
    previous_source,
    previous_translation,
    system_prompt: str,
):
    results = []
    failed_indices: list[int] = []
    for idx, text in enumerate(target_texts):
        ctx_before = context_before + target_texts[:idx]
        ctx_after = target_texts[idx + 1:] + context_after
        prev_src = previous_source + target_texts[:idx]
        prev_tgt = previous_translation + results[:idx]
        single = None
        for attempt in range(3):
            try:
                out = translate_batch(
                    [text], ctx_before, ctx_after,
                    previous_source=prev_src,
                    previous_translation=prev_tgt,
                    system_prompt=system_prompt,
                )
                single = out[0]
                break
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError):
                if attempt < 2:
                    time.sleep(2)
        if single is None:
            print(f"    Zeile {idx} nicht übersetzbar, übernehme Original: {text!r}")
            single = text
            failed_indices.append(idx)
        results.append(single)
    return results, failed_indices


def load_subtitle_source(input_path: Path):
    suffix = input_path.suffix.lower()

    if suffix in SUBTITLE_EXTENSIONS:
        print(f"Erkenne Untertiteldatei: {input_path.name}")
        raw = input_path.read_text(encoding="utf-8-sig")
        subs = pysubs2.SSAFile.from_string(raw)
        return subs, suffix

    if suffix in VIDEO_EXTENSIONS:
        print(f"Erkenne Videodatei: {input_path.name}")
        if suffix in MKV_FAMILY_EXTENSIONS:
            # FIX 5: Bei mkvmerge-Fehler (Container nicht lesbar) auf
            # ffmpeg-Fallback ausweichen. Aussagekräftige RuntimeErrors
            # (z. B. "nur Bilduntertitel") werden NICHT abgefangen.
            try:
                info = inspect_video(input_path)
                track = find_english_text_track(info)
                print(f"\nVerwende Track {track['id']} ({track.get('codec')}).")
                raw, extension = extract_track(input_path, track)
                subs = pysubs2.SSAFile.from_string(raw)
                return subs, extension
            except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
                print(f"  mkvmerge-Fehler ({exc}) – versuche ffmpeg-Fallback …")
                return load_subtitle_source_via_ffmpeg(input_path)
        return load_subtitle_source_via_ffmpeg(input_path)

    raise SystemExit(
        f"Unbekanntes Dateiformat: {suffix}. "
        f"Unterstützt: {SUBTITLE_EXTENSIONS | VIDEO_EXTENSIONS}"
    )


def main():
    if len(sys.argv) < 2 or len(sys.argv) > 3:
        print(
            "Verwendung:\n"
            "  docker compose run --rm -it translator /work/input/film.mkv\n"
            "  docker compose run --rm -it translator /work/input/film.mkv \"Suchbegriff\"\n"
            "  docker compose run --rm -it translator /work/input/film.srt\n"
            "  docker compose run --rm -it translator /work/input/film.ass\n"
            "  docker compose run --rm -it translator /work/input/film.mp4"
        )
        sys.exit(2)

    input_path = Path(sys.argv[1])
    if not input_path.is_file():
        raise SystemExit(f"Datei nicht gefunden: {input_path}")

    # NEU: Optionaler manueller Suchbegriff als 2. Argument
    manual_title = sys.argv[2].strip() if len(sys.argv) >= 3 else None

    output_dir = Path("/work/output")
    output_dir.mkdir(parents=True, exist_ok=True)

    # Zuerst Eingabedatei und Untertitel prüfen.
    # Bei einem Fehler wird keine unnötige AniList-Suche gestartet.
    subs, source_extension = load_subtitle_source(input_path)

    if manual_title:
        print(f"  Verwende manuellen Suchbegriff: {manual_title!r}")
        anime_title, anilist_meta = _search_loop(
            manual_title, default_fallback=manual_title
        )
    else:
        anime_title, anilist_meta = resolve_anime_title(input_path)

    if anime_title:
        print(f"  Serientitel: {anime_title!r}")
    else:
        print("  Kein Serientitel erkannt. Übersetze ohne Serienkontext.")

    if anilist_meta:
        genres = anilist_meta.get("genres", [])
        has_desc = bool(anilist_meta.get("description"))
        print(
            f"  AniList: {len(genres)} Genres, "
            f"Synopsis {'vorhanden' if has_desc else 'leer'}."
        )
    elif anime_title:
        print("  AniList: Kein Treffer. Übersetze nur mit Titel, ohne Synopsis.")

    system_prompt = build_system_prompt(anime_title, anilist_meta)

    is_ass = source_extension in {".ass", ".ssa"}

    original_count = len(subs.events)
    subs.events = dedupe_events(subs.events)
    print(f"  Nach Duplikat-Entfernung: {len(subs.events)} (vorher {original_count})")

    valid_events = [e for e in subs.events if event_is_valid(e, is_ass=is_ass)]
    removed = len(subs.events) - len(valid_events)
    if removed:
        print(f"  Entfernte Müll-Events: {removed}")
    subs.events = valid_events

    if not subs.events:
        raise RuntimeError("Keine gültigen Dialog-Events gefunden.")

    if is_ass:
        SIGN_STYLES = {"Signs", "Sign", "OP", "ED", "Title", "Song", "Karaoke"}

        def is_sign_event(event) -> bool:
            style = getattr(event, "style", "") or ""
            return style in SIGN_STYLES

        dialog_events = [e for e in subs.events if not is_sign_event(e)]
        sign_events = [e for e in subs.events if is_sign_event(e)]
        print(f"  Sign-/Titel-Events (nicht übersetzen): {len(sign_events)}")
    else:
        dialog_events = list(subs.events)
        sign_events = []

    print(f"  Dialog-Events (übersetzen): {len(dialog_events)}")

    if not dialog_events:
        raise RuntimeError("Keine Dialog-Events zum Übersetzen gefunden.")

    original_texts = [clean_source_text(e.plaintext, is_ass=is_ass) for e in dialog_events]

    if any(not t for t in original_texts):
        bad = [i for i, t in enumerate(original_texts) if not t]
        raise RuntimeError(f"Bereinigte Texte leer an Positionen: {bad[:10]}")

    total_batches = (len(dialog_events) + BATCH_SIZE - 1) // BATCH_SIZE

    print(f"\n{len(dialog_events)} Dialog-Untertitel werden übersetzt.")
    print(f"Modell: {MODEL}")
    print(f"Batch-Größe: {BATCH_SIZE}, Kontext: ±{CONTEXT_BEFORE} Zeilen")
    print(f"Vorherige Übersetzungen als Kontext: max. {MAX_PREVIOUS_TRANSLATIONS}")
    print(f"Idiom-Fixes aktiv: {len(IDIOM_FIXES)}")
    print(f"Quellformat: {source_extension} (ASS-Filter: {is_ass})")
    if anilist_meta:
        print(f"Serienkontext im Prompt: Ja (AniList)")
    elif anime_title:
        print(f"Serienkontext im Prompt: Ja (nur Titel, keine API)")
    else:
        print(f"Serienkontext im Prompt: Nein")
    print()

    translated_history: list[str] = []
    all_failed_indices: list[int] = []

    # FIX 1: Zeilenumbruch immer \N – pysubs2 konvertiert beim Speichern
    # ins jeweilige Zielformat (SRT/VTT → \n, ASS/SSA → \N).
    line_sep = r"\N"

    for batch_index, start in enumerate(range(0, len(dialog_events), BATCH_SIZE), start=1):
        end = min(start + BATCH_SIZE, len(dialog_events))
        target_texts = original_texts[start:end]

        before_start = max(0, start - CONTEXT_BEFORE)
        after_end = min(len(original_texts), end + CONTEXT_AFTER)
        context_before = original_texts[before_start:start]
        context_after = original_texts[end:after_end]

        previous_source = original_texts[:start]
        previous_translation = translated_history[:]

        print(
            f"Block {batch_index}/{total_batches}: "
            f"{len(target_texts)} Zielzeilen, "
            f"{len(context_before)} vorher, "
            f"{len(context_after)} nachher, "
            f"{min(len(previous_translation), MAX_PREVIOUS_TRANSLATIONS)} prev-trans"
        )

        translations = None
        last_error = None
        for attempt in range(3):
            try:
                translations = translate_batch(
                    target_texts,
                    context_before,
                    context_after,
                    previous_source=previous_source,
                    previous_translation=previous_translation,
                    system_prompt=system_prompt,
                )
                last_error = None
                break
            except (requests.RequestException, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
                if attempt < 2:
                    print(f"  Fehler: {exc}. Neuer Versuch ...")
                    time.sleep(2)

        if translations is None:
            print(f"  Batch-Wiederholung fehlgeschlagen ({last_error}). Zeilenweiser Fallback ...")
            translations, failed = _translate_line_by_line(
                target_texts,
                context_before,
                context_after,
                previous_source=previous_source,
                previous_translation=previous_translation,
                system_prompt=system_prompt,
            )
            all_failed_indices.extend(start + i for i in failed)

        for event, translated in zip(dialog_events[start:end], translations):
            event.text = wrap_subtitle(translated, line_sep=line_sep)

        translated_history.extend(translations)

    if source_extension in {".ass", ".ssa"}:
        out_format = "ass"
    elif source_extension == ".vtt":
        out_format = "vtt"
    else:
        out_format = "srt"

    out = output_dir / f"{input_path.stem}.de.{out_format}"
    if out.exists():
        raise FileExistsError(
            f"Die Ausgabedatei existiert bereits: {out}\n"
            "Bitte sichere oder verschiebe die Datei zuerst, "
            "damit sie nicht versehentlich überschrieben wird."
        )
    subs.save(str(out), format_=out_format, encoding="utf-8")

    check = pysubs2.load(str(out), encoding="utf-8")
    checked_events = [e for e in check.events if e.text.strip()]
    if len(checked_events) != len(subs.events):
        raise RuntimeError(
            f"Validierung: Anzahl nicht-leerer Events stimmt nicht überein "
            f"({len(checked_events)} statt {len(subs.events)})."
        )

    checked_times = [(e.start, e.end) for e in check.events]
    expected_times = [(e.start, e.end) for e in subs.events]
    if checked_times != expected_times:
        raise RuntimeError("Validierung: Zeitstempel wurden verändert.")

    for i, e in enumerate(check.events):
        if not e.text.strip():
            raise RuntimeError(f"Validierung: Zeile {i} ist leer.")

    print(f"\n✓ Fertig: {out}")
    print(f"  Dialog-Untertitel übersetzt: {len(dialog_events)}")
    if is_ass:
        print(f"  Sign-/Titel-Events unverändert: {len(sign_events)}")
    print(f"  Gespeicherte Events: {len(subs.events)}")
    print(f"  Zeitstempel geprüft: OK")
    print(f"  Ausgabeformat: {out_format.upper()}")

    if all_failed_indices:
        unique_failed = sorted(set(all_failed_indices))

        print(
            f"\nWARNUNG: {len(unique_failed)} von "
            f"{len(dialog_events)} Dialogzeilen konnten nicht "
            "zuverlässig übersetzt werden."
        )
        print("Die folgenden Zeilen wurden im Original übernommen:")

        for idx in unique_failed:
            print(f"  - Zeile {idx + 1}: {original_texts[idx][:100]!r}")

        print(f"\nDie Datei wurde gespeichert: {out}")

        if STRICT_TRANSLATION:
            print(
                "Strenger Modus aktiv: "
                "Rückgabecode 1 wegen unvollständiger Übersetzung."
            )
            sys.exit(1)
    else:
        print("Übersetzungsstatus: Alle Dialogzeilen erfolgreich verarbeitet.")


if __name__ == "__main__":
    main()