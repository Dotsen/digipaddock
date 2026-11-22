"""The speech recognition probe: faster-whisper small against medium on the stage CPU.

Steps on the stage host (docs/superpowers/kd9075-whisper-probe-protocol.md has the full order):

``collect`` (root) copies the owner's recordings from a card's attachments (the cabinet base opened read-only, the
files from the cabinet's file store) into ``items.json`` + ``<item><ext>``: ten phrases ``p01``…``p10`` or all of
them read in a row in one recording ``phrases``, and three free messages ``free1``…``free3``, by upload order or by
``--map <file name>=<item>``.

``vocab`` (root) writes the dictionary the cabinet knows — agents, projects, servers of cards, open card ids — as
``vocab.json``; with it the model gets a hint (``initial_prompt``, whole terms within the 223 prompt tokens
whisper keeps; each row records the hint it got), without it none.

``fetch`` (network, once) downloads the models into ``--models-dir``; ``run`` works offline from there.

``run`` transcribes every item with every model, with and without the dictionary, each in its own process
(``one``) — as the one-shot executor of voice 1 would: the wall time includes loading the model, the peak memory
is the child's own maximum RSS. Rows go to ``runs.jsonl`` as they come.

``summary`` scores the rows against the marked phrases (``--phrases``: ``[keyword]`` in the text) and the owner's
confirmed transcripts of the free messages (``<item>.ref.txt`` next to the recordings, keywords marked the same
way; a free message without one is not scored yet) and prints the table and the verdict per model and mode.

A card id or a number (``KD-9032``, ``502``) counts when its digits stand alone in the transcript (``КХ 9032``,
``9 032``); any keyword (``node-a1`` too) also counts as a whole-word match of itself or a listed variant (``variants`` of the
phrases file: recognizable Cyrillic spellings), case, ``ё``/``э`` and punctuation aside.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from kd import ids

CABINET_DB = "/var/lib/digipaddock/cabinet/cabinet.db"
FILES_DIR = "/var/lib/digipaddock/cabinet/files"
CARD = ids.CARD
MODELS = ("small", "medium", "large-v3-turbo")
MODES = ("plain", "vocab")
PHRASES = tuple(f"p{n:02d}" for n in range(1, 11))
FREE = ("free1", "free2", "free3")
ITEMS = PHRASES + FREE
JOINED = "phrases"  # all ten phrases in one recording, read in a row (the owner's way, 20.10.2026)
KNOWN = ITEMS + (JOINED,)
# The upload order without --map, by the number of recordings: phrases apart or in one, the free messages after.
LAYOUTS = {len(ITEMS): ITEMS, 1 + len(FREE): (JOINED, *FREE), 1: (JOINED,)}
AUDIO_EXT = {".m4a", ".mp3", ".mp4", ".ogg", ".oga", ".opus", ".wav", ".webm", ".aac", ".flac", ".amr", ".3gp"}
MIME_EXT = {
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/aac": ".aac",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/webm": ".webm",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
    "audio/amr": ".amr",
    "audio/3gpp": ".3gp",
}
# The pass criterion the owner agreed on 19.10.2026: the keywords. Memory and speed are reported only: the
# owner lifted their limits on 21.10.2026 (the model is loaded per voice message, not kept in memory).
MIN_KEYWORDS = 0.9
MAX_PHRASE_MISSES = 3
# Whisper keeps only the last 223 tokens of a prompt (faster-whisper get_prompt: max_length // 2 - 1): the child
# builds the hint from the dictionary in its model's tokens, whole terms, card ids last (they count by digits anyway).
PROMPT_TOKENS = 223
OPEN_CARDS = 40

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class ProbeError(ValueError):
    """A step that must not run: a missing recording, a changed file, a used output folder."""


# --- keywords --------------------------------------------------------------------------------------------------

MARK = re.compile(r"\[([^\[\]]+)\]")


def marked(text: str) -> tuple[str, list[str]]:
    """``Check [disk] on [example-prod]`` → the plain text and its keywords in order."""
    return MARK.sub(lambda m: m.group(1), text), [m.group(1).strip() for m in MARK.finditer(text)]


def normalize(text: str) -> str:
    text = text.lower().replace("ё", "е").replace("э", "е")
    text = re.sub(r"[^\w\s]|_", " ", text)
    return " ".join(text.split())


def _digits_only(text: str) -> str:
    """A thousands group split by a space, comma or dash is one number (``9 032`` → ``9032``); other digits stay
    apart — a card id and the next item's number (``KD-<n> 9. nginx``), an item's number and a card id
    (``3. 932``) must not merge. A transcript corrected by synonyms has lost its dots: there ``3 932`` merges."""
    return re.sub(r"(?<=\d)[\s,\-–—]+(?=\d{3}(?!\d))", "", text)


NUMBER = re.compile(r"(?:KD-)?([0-9]+)", re.IGNORECASE)


def found(keyword: str, transcript: str, variants: Mapping[str, Sequence[str]] = {}) -> bool:
    number = NUMBER.fullmatch(keyword.strip())  # a card id or a number counts by its digits; node-a1 does not
    if number and re.search(rf"(?<!\d){number.group(1)}(?!\d)", _digits_only(transcript)):
        return True
    tokens = normalize(transcript).split()
    # A form matches whole words, also split or joined differently: "journal ctl" for journalctl, "stageprod".
    windows = {"".join(tokens[i:j]) for i in range(len(tokens)) for j in range(i + 1, min(i + 4, len(tokens)) + 1)}
    return any(normalize(form).replace(" ", "") in windows for form in (keyword, *variants.get(keyword, ())))


def keyword_score(reference: str, transcript: str, variants: Mapping[str, Sequence[str]] = {}) -> dict:
    """Hits and misses of a marked reference's keywords in a transcript."""
    _, keywords = marked(reference)
    misses = [word for word in keywords if not found(word, transcript, variants)]
    return {"total": len(keywords), "hits": len(keywords) - len(misses), "misses": misses}


def load_synonyms(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text("utf-8"))
    synonyms = data.get("synonyms") if isinstance(data, dict) else None
    if not isinstance(synonyms, dict) or not all(
        normalize(word) and isinstance(forms, list) and forms
        and all(isinstance(form, str) and normalize(form) for form in forms)
        for word, forms in synonyms.items()
    ):
        raise ProbeError(f"{path}: synonyms map a word to a non-empty list of misheard forms")
    return synonyms


def correct(transcript: str, synonyms: Mapping[str, Sequence[str]]) -> str:
    """The transcript with every misheard form (whole words, case, ё/э and punctuation aside) replaced by its word,
    longer forms first; the result is normalized text."""
    tokens = normalize(transcript).split()
    pairs = sorted(
        ((normalize(form).split(), normalize(word).split()) for word, forms in synonyms.items() for form in forms),
        key=lambda pair: -len(pair[0]),
    )
    for form, word in pairs:
        i, out = 0, []
        while i < len(tokens):
            if tokens[i:i + len(form)] == form:
                out += word
                i += len(form)
            else:
                out.append(tokens[i])
                i += 1
        tokens = out
    return " ".join(tokens)


def wer(reference: str, transcript: str) -> float:
    """Word error rate of a transcript against a reference (markup and punctuation aside)."""
    ref = normalize(marked(reference)[0]).split()
    hyp = normalize(transcript).split()
    if not ref:
        return 0.0 if not hyp else 1.0
    row = list(range(len(hyp) + 1))
    for i, word in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, other in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (word != other))
    return row[-1] / len(ref)


def load_phrases(path: Path) -> tuple[dict[str, str], dict[str, list[str]]]:
    data = json.loads(path.read_text("utf-8"))
    phrases, variants = data.get("phrases"), data.get("variants", {})
    if not isinstance(phrases, dict) or sorted(phrases) != list(PHRASES):
        raise ProbeError(f"{path}: phrases must be exactly {', '.join(PHRASES)}")
    if not all(isinstance(text, str) and marked(text)[1] for text in phrases.values()):
        raise ProbeError(f"{path}: every phrase marks its keywords in [brackets]")
    if not isinstance(variants, dict) or not all(
        isinstance(v, list) and all(isinstance(s, str) for s in v) for v in variants.values()
    ):
        raise ProbeError(f"{path}: variants map a keyword to a list of spellings")
    return phrases, variants


# --- collect (root) --------------------------------------------------------------------------------------------


def _card(value: str) -> str:
    if not CARD.fullmatch(value):
        raise ProbeError(f"not a card id: {value}")
    return value


def _ext(name: str, mime: str) -> str:
    suffix = Path(name).suffix.lower()
    return suffix if suffix in AUDIO_EXT else MIME_EXT.get(mime.split(";")[0].strip().lower(), "")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fresh(out: Path) -> None:
    if out.exists() and any(out.iterdir()):
        raise ProbeError(f"{out} is not empty: give a new folder")
    out.mkdir(parents=True, exist_ok=True)


def collect(db: str, files_dir: str, card: str, out: Path, mapping: Mapping[str, str] = {}) -> list[dict]:
    """Copy a card's audio attachments into ``out`` as the probe items. Without ``mapping`` the upload order is
    the item order of ``LAYOUTS`` (13 recordings: ten phrases and three free messages; 4: the phrases in one
    recording and the free messages; 1: the phrases only); a mapping names the items it takes by the file's name."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT a.id, a.name, a.mime, a.sha256, a.seq FROM attachments a JOIN threads t ON a.thread = t.id"
            " WHERE t.card = ? ORDER BY a.seq, a.rowid",
            (_card(card),),
        ).fetchall()
    finally:
        conn.close()
    audio = [row for row in rows if row[2].lower().startswith("audio/") or _ext(row[1], row[2])]
    if mapping:
        items = list(mapping.values())
        if len(set(items)) != len(items) or (JOINED in items and set(items) & set(PHRASES)):
            raise ProbeError(f"--map names each item once, and the phrases either apart or as {JOINED}")
        by_name: dict[str, tuple] = {}
        for row in audio:
            if row[1] in mapping:
                if row[1] in by_name:
                    raise ProbeError(f"two attachments are named {row[1]}: upload one of them again")
                by_name[row[1]] = row
        missing = sorted(set(mapping) - set(by_name))
        if missing:
            raise ProbeError(f"no audio attachment named {', '.join(missing)}")
        chosen = [(mapping[name], by_name[name]) for name in mapping]
    else:
        if len(audio) not in LAYOUTS:
            names = ", ".join(f"{row[1]} (#{row[4]})" for row in audio) or "none"
            counts = ", ".join(str(count) for count in sorted(LAYOUTS, reverse=True))
            raise ProbeError(f"{card} has {len(audio)} audio attachments, not {counts}: {names}; use --map")
        chosen = list(zip(LAYOUTS[len(audio)], audio))
        print(
            f"{len(audio)} recordings taken by upload order as {', '.join(LAYOUTS[len(audio)])}: check against the"
            " thread, else --map",
            file=sys.stderr,
        )
    _fresh(out)
    items = []
    for item, (file_id, name, mime, sha256, seq) in sorted(chosen):
        source = Path(files_dir) / file_id
        if not re.fullmatch(r"[A-Za-z0-9_-]+", file_id) or _sha256(source) != sha256:
            raise ProbeError(f"{name}: the stored file does not match its attachment record")
        target = out / f"{item}{_ext(name, mime) or '.audio'}"
        shutil.copyfile(source, target)
        items.append({"item": item, "file": target.name, "name": name, "seq": seq, "sha256": sha256})
    (out / "items.json").write_text(json.dumps(items, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return items


def load_items(directory: Path) -> list[dict]:
    items = json.loads((directory / "items.json").read_text("utf-8"))
    for entry in items:
        if entry.get("item") not in KNOWN or _sha256(directory / entry["file"]) != entry.get("sha256"):
            raise ProbeError(f"items.json: {entry.get('item')} is unknown or its recording changed")
    return items


# --- vocab (root) ----------------------------------------------------------------------------------------------


def vocab(db: str, agent_names: Iterable[str], open_cards: int = OPEN_CARDS) -> dict[str, list[str]]:
    """What the cabinet knows: agents, projects (id and name), servers named on cards, the latest open cards."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return vocab_from(conn, agent_names, open_cards)
    finally:
        conn.close()


def vocab_from(conn: sqlite3.Connection, agent_names: Iterable[str], open_cards: int = OPEN_CARDS) -> dict[str, list[str]]:
    """``vocab`` over an open connection to the cabinet's base (the cabinet's own)."""
    assignees = [row[0].lstrip("@") for row in conn.execute("SELECT DISTINCT assignee FROM cards")]
    projects = [term for row in conn.execute("SELECT id, name FROM projects ORDER BY id") for term in row]
    servers = [row[0] for row in conn.execute("SELECT DISTINCT server FROM card_servers ORDER BY server")]
    cards = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM cards WHERE status != 'Done' ORDER BY updated DESC LIMIT ?",
            (open_cards,),
        )
    ]

    def unique(terms: Iterable[str]) -> list[str]:
        seen: dict[str, None] = {}
        for term in terms:
            if term and term != "owner" and term.lower() not in {key.lower() for key in seen}:
                seen[term] = None
        return list(seen)

    return {
        "agents": unique([*agent_names, *sorted(assignees)]),
        "projects": unique(projects),
        "servers": unique(servers),
        "cards": unique(cards),
    }


def prompt(terms: Mapping[str, Sequence[str]], fits: Callable[[str], bool]) -> str:
    """The hint for the model: Russian labels, the terms as they are written, as many whole terms as ``fits``
    allows — agents, servers and projects before card ids."""
    labels = (("agents", "Агенты"), ("servers", "Серверы"), ("projects", "Проекты"), ("cards", "Карточки"))
    parts: list[str] = []
    for key, label in labels:
        kept: list[str] = []
        for term in terms.get(key, ()):
            if not fits(" ".join(parts + [f"{label}: {', '.join(kept + [term])}."])):
                break
            kept.append(term)
        if kept:
            parts.append(f"{label}: {', '.join(kept)}.")
    return " ".join(parts)


# --- fetch, one (in the probe's venv) --------------------------------------------------------------------------


def fetch(models: Sequence[str], models_dir: Path) -> None:
    from faster_whisper import WhisperModel  # the probe venv only

    for name in models:
        WhisperModel(name, device="cpu", compute_type="int8", download_root=str(models_dir))


def transcribe_one(args: argparse.Namespace) -> dict:
    """One recording in this process, as the executor would do it: load, transcribe, report, exit."""
    import resource

    started = time.monotonic()
    import ctranslate2
    import faster_whisper
    from faster_whisper import WhisperModel

    model = WhisperModel(
        args.model, device="cpu", compute_type="int8", cpu_threads=args.threads,
        download_root=str(args.models_dir), local_files_only=True,
    )
    loaded = time.monotonic()
    hint, hint_tokens = "", 0
    if args.vocab:

        def tokens(text: str) -> int:  # as faster-whisper encodes an initial prompt
            return len(model.hf_tokenizer.encode(" " + text.strip(), add_special_tokens=False).ids)

        hint = prompt(json.loads(args.vocab.read_text("utf-8")), lambda text: tokens(text) <= PROMPT_TOKENS)
        hint_tokens = tokens(hint) if hint else 0
    segments, info = model.transcribe(
        str(args.audio), language=args.language, beam_size=args.beam, vad_filter=True, initial_prompt=hint or None,
    )
    text = " ".join(segment.text.strip() for segment in segments)  # the segments are decoded while iterated
    done = time.monotonic()
    return {
        "prompt": hint,
        "prompt_tokens": hint_tokens,
        "text": text,
        "duration": round(info.duration, 2),
        "load_s": round(loaded - started, 2),
        "transcribe_s": round(done - loaded, 2),
        "peak_rss": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024,
        "versions": {"faster_whisper": faster_whisper.__version__, "ctranslate2": ctranslate2.__version__},
    }


# --- run -------------------------------------------------------------------------------------------------------


def run(
    *, directory: Path, items: Sequence[Mapping[str, Any]], out: Path, models: Sequence[str],
    modes: Sequence[str], vocab_file: Path | None, child: Sequence[str], models_dir: Path, language: str,
    threads: int, beam: int, timeout: int, runner: Runner = subprocess.run,
) -> list[dict]:
    if vocab_file is None and "vocab" in modes:
        raise ProbeError("mode vocab needs a dictionary (--vocab)")
    _fresh(out)
    env = {**os.environ, "HF_HUB_OFFLINE": "1"}
    rows = []
    for model in models:
        for mode in modes:
            for entry in items:
                command = [
                    *child, "one", "--model", model, "--models-dir", str(models_dir),
                    "--audio", str(directory / entry["file"]), "--language", language,
                    "--threads", str(threads), "--beam", str(beam),
                ] + (["--vocab", str(Path(vocab_file).resolve())] if mode == "vocab" and vocab_file else [])
                row: dict[str, Any] = {
                    "model": model, "mode": mode, "item": entry["item"], "threads": threads, "beam": beam,
                }
                started = time.monotonic()
                try:
                    done = runner(command, capture_output=True, text=True, timeout=timeout, env=env)
                    row["wall_s"] = round(time.monotonic() - started, 2)
                    lines = done.stdout.strip().splitlines()
                    if done.returncode != 0 or not lines:
                        row["error"] = (done.stderr.strip().splitlines() or [f"exit {done.returncode}"])[-1]
                    else:
                        try:
                            result = json.loads(lines[-1])
                        except ValueError:
                            result = None
                        if isinstance(result, dict):
                            row.update(result)
                        else:
                            row["error"] = f"no result line: {lines[-1][:200]}"
                except subprocess.TimeoutExpired:
                    row["wall_s"], row["error"] = round(time.monotonic() - started, 2), f"timeout {timeout}s"
                with (out / "runs.jsonl").open("a", encoding="utf-8") as journal:
                    journal.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"{model}\t{mode}\t{entry['item']}\t{row.get('wall_s')}s\t{row.get('error', 'ok')}", flush=True)
                rows.append(row)
    return rows


# --- summary ---------------------------------------------------------------------------------------------------


def _gib(value: float) -> str:
    return f"{value / 1024**3:.2f}"


def summary(
    rows: Iterable[Mapping[str, Any]], phrases: Mapping[str, str], references: Mapping[str, str],
    variants: Mapping[str, Sequence[str]] = {}, synonyms: Mapping[str, Sequence[str]] | None = None,
) -> str:
    """Per model and mode: keywords on the phrases and on the scored free messages (the verdict), and for
    reference the speed on the free messages and on the phrases read in one recording (one-shot wall time /
    length) and the peak memory; then the misses to review. With ``synonyms`` every transcript is corrected first."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        if synonyms is not None and "text" in row:  # the misses below still quote what the model heard
            row = {**row, "text": correct(row["text"], synonyms), "heard": row["text"]}
        groups.setdefault((row["model"], row["mode"]), []).append(row)
    head = (
        "| модель | словарь | фразы: верно | ошибок | свободные: верно | WER свободных | всего | "
        "скорость (время/длина) | пик, ГиБ | ошибки запуска | итог |"
    )
    lines = [head, "|" + "---|" * 11]
    details: list[str] = []
    def order(key: tuple[str, str]) -> tuple[int, str, str]:
        return (MODELS.index(key[0]) if key[0] in MODELS else len(MODELS), *key)

    for (model, mode), group in sorted(groups.items(), key=lambda pair: order(pair[0])):
        by_item = {row["item"]: row for row in group}
        failed = [row for row in group if row.get("error")]
        phrase_hits = phrase_total = free_hits = free_total = 0
        wers, speed = [], []
        # The phrases apart (p01…p10) or read in a row in one recording: then all 33 keywords against its text.
        joined = JOINED in by_item
        targets = [(JOINED, " ".join(phrases[item] for item in PHRASES))] if joined else list(phrases.items())
        for item, reference in targets:
            score = keyword_score(reference, by_item.get(item, {}).get("text", ""), variants)
            phrase_hits, phrase_total = phrase_hits + score["hits"], phrase_total + score["total"]
            if score["misses"]:
                text = by_item.get(item, {}).get("heard", by_item.get(item, {}).get("text", ""))
                text = text if len(text) <= 300 else text[:300] + "…"  # the phrases in one recording: a minute of text
                details.append(f"- {model}/{mode} {item}: нет {', '.join(score['misses'])} — «{text}»")
        if joined and by_item[JOINED].get("duration"):  # a real stretch of speech: its speed counts too
            row = by_item[JOINED]
            speed.append((row["wall_s"] / row["duration"], row["duration"], JOINED))
        for item in FREE:
            row = by_item.get(item, {})
            if row.get("duration"):
                speed.append((row["wall_s"] / row["duration"], row["duration"], item))
            if item in references and not row.get("error"):
                score = keyword_score(references[item], row.get("text", ""), variants)
                free_hits, free_total = free_hits + score["hits"], free_total + score["total"]
                wers.append(wer(references[item], row.get("text", "")))
                if score["misses"]:
                    details.append(f"- {model}/{mode} {item}: нет {', '.join(score['misses'])}")
        hits, total = phrase_hits + free_hits, phrase_total + free_total
        share = hits / total if total else 0.0
        peak = max((row.get("peak_rss", 0) for row in group), default=0)
        has_phrases = joined or set(PHRASES) <= set(by_item)
        complete = has_phrases and set(FREE) <= set(by_item) and not failed and len(references) == len(FREE)
        passed = phrase_total - phrase_hits <= MAX_PHRASE_MISSES and share >= MIN_KEYWORDS and not failed
        verdict = ("проходит" if passed else "не проходит") + ("" if complete else " (неполно)")
        speeds = ", ".join(f"{item} {duration:.0f} с: {ratio:.2f}" for ratio, duration, item in speed) or "-"
        cells = [
            model, "да" if mode == "vocab" else "нет", f"{phrase_hits}/{phrase_total}", str(phrase_total - phrase_hits),
            f"{free_hits}/{free_total}", f"{sum(wers) / len(wers):.2f}" if wers else "-", f"{share:.0%}", speeds,
            _gib(peak), str(len(failed)), verdict,
        ]
        lines.append("| " + " | ".join(cells) + " |")
    missing = [item for item in FREE if item not in references]
    notes = ["Итог — по ключевым словам; скорость и память справочно (решение владельца 21.10)."]
    if synonyms is not None:
        notes.append(f"Расшифровки исправлены по синонимам ({len(synonyms)} слов).")
    if missing:
        notes.append(f"Свободные без подтверждённого эталона (не оценены): {', '.join(missing)}.")
    return "\n".join(lines + [""] + notes + (["Промахи:"] + details if details else [])) + "\n"


def load_references(directory: Path) -> dict[str, str]:
    return {
        item: (directory / f"{item}.ref.txt").read_text("utf-8")
        for item in FREE
        if (directory / f"{item}.ref.txt").exists()
    }


# --- entry -----------------------------------------------------------------------------------------------------


def _mapping(values: Sequence[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, sep, item = value.rpartition("=")
        if not sep or not name or item not in KNOWN or name in result:
            raise ProbeError(f"expected <file name>=<item> once per file, got {value}")
        result[name] = item
    return result


def main(argv_: Sequence[str] | None = None, *, runner: Runner = subprocess.run, script: str = "") -> int:
    parser = argparse.ArgumentParser(prog="whisper_probe")
    sub = parser.add_subparsers(dest="command", required=True)
    col = sub.add_parser("collect", help="root: the owner's recordings from a card's attachments")
    col.add_argument("--card", required=True)
    col.add_argument("--out", required=True, type=Path)
    col.add_argument("--db", default=CABINET_DB)
    col.add_argument("--files", default=FILES_DIR)
    col.add_argument("--map", action="append", default=[], help="<attachment name>=<p01..p10|phrases|free1..free3>")
    voc = sub.add_parser("vocab", help="root: the cabinet's dictionary as JSON")
    voc.add_argument("--db", default=CABINET_DB)
    voc.add_argument("--open-cards", type=int, default=OPEN_CARDS)
    get = sub.add_parser("fetch", help="once, with network: download the models")
    get.add_argument("--models", default=",".join(MODELS))
    get.add_argument("--models-dir", required=True, type=Path)
    one = sub.add_parser("one", help="internal: one recording in this process")
    one.add_argument("--model", required=True)
    one.add_argument("--models-dir", required=True, type=Path)
    one.add_argument("--audio", required=True, type=Path)
    one.add_argument("--language", default="ru")
    one.add_argument("--threads", type=int, default=4)
    one.add_argument("--beam", type=int, default=5)
    one.add_argument("--vocab", type=Path, help="vocab.json: the hint, cut to the model's prompt tokens")
    go = sub.add_parser("run", help="every item × model × mode, each in its own process")
    go.add_argument("--items", required=True, type=Path)
    go.add_argument("--out", required=True, type=Path)
    go.add_argument("--models-dir", required=True, type=Path)
    go.add_argument("--vocab", type=Path, help="vocab.json; without it only mode plain")
    go.add_argument("--models", default=",".join(MODELS))
    go.add_argument("--language", default="ru")
    go.add_argument("--threads", type=int, default=4)
    go.add_argument("--beam", type=int, default=5)
    go.add_argument("--timeout", type=int, default=1800)
    table = sub.add_parser("summary", help="the table and the verdict of runs.jsonl")
    table.add_argument("runs", type=Path)
    table.add_argument("--phrases", required=True, type=Path)
    table.add_argument("--items", required=True, type=Path, help="the recordings' folder with <item>.ref.txt")
    table.add_argument("--synonyms", type=Path, help="correct the transcripts by these misheard forms first")
    args = parser.parse_args(argv_)
    try:
        if args.command == "collect":
            items = collect(args.db, args.files, args.card, args.out, _mapping(args.map))
            print(json.dumps(items, indent=2, ensure_ascii=False))
        elif args.command == "vocab":
            from kd import agents

            print(json.dumps(vocab(args.db, agents.names(), args.open_cards), indent=2, ensure_ascii=False))
        elif args.command == "fetch":
            fetch(args.models.split(","), args.models_dir)
        elif args.command == "one":
            print(json.dumps(transcribe_one(args), ensure_ascii=False))
        elif args.command == "run":
            if args.vocab is not None and not isinstance(json.loads(args.vocab.read_text("utf-8")), dict):
                raise ProbeError(f"{args.vocab}: not a dictionary of terms")
            run(
                directory=args.items, items=load_items(args.items), out=args.out, models=args.models.split(","),
                modes=MODES if args.vocab is not None else ("plain",), vocab_file=args.vocab,
                child=[sys.executable, "-I", script or sys.argv[0]], models_dir=args.models_dir,
                language=args.language, threads=args.threads, beam=args.beam, timeout=args.timeout, runner=runner,
            )
        else:
            phrases, variants = load_phrases(args.phrases)
            rows = [json.loads(line) for line in args.runs.read_text("utf-8").splitlines() if line.strip()]
            synonyms = load_synonyms(args.synonyms) if args.synonyms else None
            sys.stdout.write(summary(rows, phrases, load_references(args.items), variants, synonyms))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:  # ProbeError and broken JSON are ValueErrors
        print(f"whisper_probe: {exc}", file=sys.stderr)
        return 1
