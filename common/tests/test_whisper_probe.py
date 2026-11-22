"""kd.whisper_probe: the owner's recordings, small against medium, with and without the dictionary."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from kd import whisper_probe as wp

REPO = Path(__file__).resolve().parents[2]
# synthetic copies: the deployment's own lists live in its overlay
PHRASES_FILE = REPO / "common" / "tests" / "fixtures" / "voice" / "phrases.json"
SYNONYMS_FILE = REPO / "common" / "tests" / "fixtures" / "voice" / "synonyms.json"


# --- keywords --------------------------------------------------------------------------------------------------


def test_marked_splits_text_and_keywords():
    assert wp.marked("Проверь [диск] на [example-prod].") == ("Проверь диск на example-prod.", ["диск", "example-prod"])


@pytest.mark.parametrize(
    "keyword, transcript",
    [
        ("KD-9032", "КХ 9032 в работу"),
        ("KD-9032", "kd-9032 в работу"),
        ("KD-9032", "9 032 в работу"),
        ("KD-228", "по ветке KN228 9. Nginx на"),  # the next item's number does not merge into the card id
        ("502", "отдаёт 502, проверь"),
        ("example-prod", "на Example Prod и"),
        ("example-prod", "на exampleprod и"),
        ("journalctl", "посмотри journal ctl по"),
        ("бэкап", "сделай бекап базы"),
        ("выкатки", "после выкатки."),
        ("git diff", "покажи Git-diff по"),
    ],
)
def test_found(keyword, transcript):
    assert wp.found(keyword, transcript)


@pytest.mark.parametrize(
    "keyword, transcript",
    [
        ("KD-9032", "КХ 90321 в работу"),
        ("KD-40", "KD-140 в Done"),
        ("17", "номер 170"),
        ("dev", "разработчик devops"),
        ("выкатки", "после выкатке"),
        ("example-prod", "на example"),
        ("node-a1", "строка 1 на node"),  # only card ids and numbers count by digits
        ("KD-9032", "по ветке KN90 32. Проект"),  # only a thousands group joins: 90 and 32 stay apart
    ],
)
def test_not_found(keyword, transcript):
    assert not wp.found(keyword, transcript)


def test_variants_count_as_the_keyword():
    assert not wp.found("dev", "исполнитель дев")
    assert wp.found("dev", "исполнитель дев", {"dev": ["дев"]})
    assert wp.found("17", "номер семнадцать", {"17": ["семнадцать"]})
    assert wp.found("node-a1", "на Node A1 и")


def test_keyword_score_lists_misses():
    score = wp.keyword_score("[KD-9032] в работу, исполнитель [dev].", "КХ 9032 в работу, исполнитель Дэн.")
    assert score == {"total": 2, "hits": 1, "misses": ["dev"]}


def test_wer():
    assert wp.wer("Проверь [диск] на сервере", "проверь диск на сервере") == 0
    assert wp.wer("раз два три четыре", "раз три четыре пять") == 0.5
    assert wp.wer("", "") == 0


def test_the_phrases_file_is_the_agreed_list():
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    assert list(phrases) == list(wp.PHRASES)
    assert sum(len(wp.marked(text)[1]) for text in phrases.values()) == 33  # 33 keywords
    keywords = {word for text in phrases.values() for word in wp.marked(text)[1]}
    assert set(variants) <= keywords
    for item, text in phrases.items():  # the plain phrase scores itself in full
        plain = wp.marked(text)[0]
        assert wp.keyword_score(text, plain, variants)["misses"] == [], item


def test_load_phrases_refuses_a_short_list(tmp_path):
    path = tmp_path / "phrases.json"
    path.write_text(json.dumps({"phrases": {"p01": "[диск]"}}), "utf-8")
    with pytest.raises(wp.ProbeError, match="exactly"):
        wp.load_phrases(path)


# --- collect and vocab -----------------------------------------------------------------------------------------


def _cabinet(tmp_path: Path, attachments: list[tuple[str, str, str, int]]) -> tuple[str, Path]:
    db, files = tmp_path / "cabinet.db", tmp_path / "files"
    files.mkdir()
    conn = sqlite3.connect(db)
    conn.executescript(
        "CREATE TABLE threads (id INTEGER PRIMARY KEY, kind TEXT, card TEXT, agent TEXT);"
        "CREATE TABLE attachments (id TEXT PRIMARY KEY, thread INTEGER, seq INTEGER, name TEXT, mime TEXT,"
        " size INTEGER, sha256 TEXT, created TEXT);"
        "CREATE TABLE projects (id TEXT PRIMARY KEY, name TEXT, route TEXT);"
        "CREATE TABLE cards (id TEXT, status TEXT, assignee TEXT, updated TEXT);"
        "CREATE TABLE card_servers (card TEXT, server TEXT);"
        "INSERT INTO threads VALUES (1, 'card', 'KD-9075', ''), (2, 'card', 'KD-1', '');"
    )
    for file_id, name, mime, seq in attachments:
        data = f"audio {file_id}".encode()
        (files / file_id).write_bytes(data)
        conn.execute(
            "INSERT INTO attachments VALUES (?, 1, ?, ?, ?, ?, ?, '')",
            (file_id, seq, name, mime, len(data), hashlib.sha256(data).hexdigest()),
        )
    conn.execute("INSERT INTO attachments VALUES ('other', 2, 1, 'x.m4a', 'audio/mp4', 1, 'zz', '')")
    conn.commit()
    conn.close()
    return str(db), files


def _recordings(count: int) -> list[tuple[str, str, str, int]]:
    return [(f"f{n:02d}", f"Запись {n}.m4a", "audio/mp4", 10 + n) for n in range(count)]


def test_collect_by_upload_order(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(13) + [("pic", "photo.jpg", "image/jpeg", 50)])
    items = wp.collect(db, str(files), "KD-9075", tmp_path / "items")
    assert [entry["item"] for entry in items] == sorted(wp.ITEMS)
    by_item = {entry["item"]: entry for entry in items}
    assert by_item["p01"]["name"] == "Запись 0.m4a" and by_item["free3"]["name"] == "Запись 12.m4a"
    assert (tmp_path / "items" / "p01.m4a").read_bytes() == b"audio f00"
    assert wp.load_items(tmp_path / "items") == json.loads((tmp_path / "items" / "items.json").read_text("utf-8"))


def test_collect_takes_browser_recordings_as_they_are(tmp_path):
    voices = [(f"v{n:02d}", f"voice-{n}", "audio/webm;codecs=opus", n) for n in range(13)]  # MediaRecorder
    db, files = _cabinet(tmp_path, voices)
    items = wp.collect(db, str(files), "KD-9075", tmp_path / "items")
    assert {entry["file"] for entry in items} == {f"{item}.webm" for item in wp.ITEMS}
    assert (tmp_path / "items" / "free3.webm").read_bytes() == b"audio v12"


@pytest.mark.parametrize(
    "count, layout", [(1, ["phrases"]), (4, ["free1", "free2", "free3", "phrases"])]
)
def test_collect_takes_the_phrases_in_one_recording(tmp_path, count, layout):
    db, files = _cabinet(tmp_path, _recordings(count))
    items = wp.collect(db, str(files), "KD-9075", tmp_path / "items")
    assert [entry["item"] for entry in items] == layout
    assert {entry["item"]: entry["name"] for entry in items}["phrases"] == "Запись 0.m4a"  # first uploaded
    assert [entry["item"] for entry in wp.load_items(tmp_path / "items")] == layout


def test_collect_map_takes_phrases_apart_or_in_one(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(3))
    with pytest.raises(wp.ProbeError, match="either apart or as phrases"):
        wp.collect(db, str(files), "KD-9075", tmp_path / "a", {"Запись 0.m4a": "phrases", "Запись 1.m4a": "p01"})
    items = wp.collect(db, str(files), "KD-9075", tmp_path / "b", {"Запись 2.m4a": "phrases", "Запись 0.m4a": "free2"})
    assert {entry["item"]: entry["name"] for entry in items} == {"phrases": "Запись 2.m4a", "free2": "Запись 0.m4a"}


def test_collect_needs_thirteen_or_a_map(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(12))
    with pytest.raises(wp.ProbeError, match="12 audio attachments"):
        wp.collect(db, str(files), "KD-9075", tmp_path / "items")


def test_collect_by_map(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(14))
    mapping = {f"Запись {n}.m4a": item for n, item in zip(range(13, 0, -1), wp.ITEMS)}
    items = {entry["item"]: entry["name"] for entry in wp.collect(db, str(files), "KD-9075", tmp_path / "o", mapping)}
    assert items["p01"] == "Запись 13.m4a" and items["free3"] == "Запись 1.m4a"


def test_collect_refuses_a_changed_file(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(13))
    (files / "f03").write_bytes(b"changed")
    with pytest.raises(wp.ProbeError, match="does not match"):
        wp.collect(db, str(files), "KD-9075", tmp_path / "items")


def test_collect_refuses_a_used_folder(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(13))
    (tmp_path / "items").mkdir()
    (tmp_path / "items" / "old").write_text("x")
    with pytest.raises(wp.ProbeError, match="not empty"):
        wp.collect(db, str(files), "KD-9075", tmp_path / "items")


def test_load_items_notices_a_changed_recording(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(13))
    wp.collect(db, str(files), "KD-9075", tmp_path / "items")
    (tmp_path / "items" / "p02.m4a").write_bytes(b"other")
    with pytest.raises(wp.ProbeError, match="p02"):
        wp.load_items(tmp_path / "items")


def test_vocab_from_the_cabinet(tmp_path):
    db, _ = _cabinet(tmp_path, [])
    conn = sqlite3.connect(db)
    conn.executescript(
        "INSERT INTO projects VALUES ('digipaddock', 'Paddock', '@dev'), ('admin', 'admin', '@sysadmin');"
        "INSERT INTO cards VALUES ('KD-1', 'Done', '@dev', '2026-10-01'), ('KD-2', 'To Do', '@owner', '2026-10-03'),"
        " ('KD-3', 'In Progress', '@analyst', '2026-10-02');"
        "INSERT INTO card_servers VALUES ('KD-1', 'example-prod'), ('KD-3', 'example-prod'), ('KD-3', 'node-d');"
    )
    conn.commit()
    conn.close()
    assert wp.vocab(db, ["dev", "sysadmin"]) == {
        "agents": ["dev", "sysadmin", "analyst"],
        "projects": ["admin", "digipaddock", "Paddock"],
        "servers": ["example-prod", "node-d"],
        "cards": ["KD-2", "KD-3"],
    }


TERMS = {"cards": ["KD-1", "KD-2"], "servers": ["example-prod"], "agents": ["dev"], "projects": ["digipaddock"]}


def test_prompt_cuts_whole_terms_card_ids_last():
    def words(limit):
        return lambda text: len(text.split()) <= limit

    full = "Агенты: dev. Серверы: example-prod. Проекты: digipaddock. Карточки: KD-1, KD-2."
    assert wp.prompt(TERMS, words(99)) == full
    assert wp.prompt(TERMS, words(8)) == "Агенты: dev. Серверы: example-prod. Проекты: digipaddock. Карточки: KD-1."
    assert wp.prompt(TERMS, words(7)) == "Агенты: dev. Серверы: example-prod. Проекты: digipaddock."
    assert wp.prompt(TERMS, words(4)) == "Агенты: dev. Серверы: example-prod."


class FakeEncoding:
    def __init__(self, text, add_special_tokens=True):
        assert add_special_tokens is False  # as faster-whisper encodes an initial prompt
        self.ids = text.split()


class FakeModel:
    instances: list = []

    def __init__(self, name, **kwargs):
        self.name, self.kwargs = name, kwargs
        self.hf_tokenizer = type("Tok", (), {"encode": staticmethod(FakeEncoding)})()
        FakeModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.transcribe_kwargs = kwargs
        segments = (type("Seg", (), {"text": text})() for text in (" Мержи и выкати", " PR 17. "))
        return segments, type("Info", (), {"duration": 3.456})()


@pytest.fixture
def fake_whisper(monkeypatch):
    import types

    module = types.ModuleType("faster_whisper")
    module.WhisperModel, module.__version__ = FakeModel, "9.9"
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(__version__="4.4"))
    monkeypatch.setattr(wp, "PROMPT_TOKENS", 8)  # the fake tokenizer: a token per word
    FakeModel.instances = []
    return FakeModel


def _one(tmp_path, *extra):
    parser_args = ["one", "--model", "small", "--models-dir", str(tmp_path), "--audio", str(tmp_path / "a.m4a"), *extra]
    return wp.main(parser_args)


def test_one_cuts_the_hint_to_the_prompt_tokens(tmp_path, fake_whisper, capsys):
    (tmp_path / "vocab.json").write_text(json.dumps(TERMS), "utf-8")
    assert _one(tmp_path, "--vocab", str(tmp_path / "vocab.json")) == 0
    row = json.loads(capsys.readouterr().out)
    model = fake_whisper.instances[0]
    assert model.kwargs == {
        "device": "cpu", "compute_type": "int8", "cpu_threads": 4, "download_root": str(tmp_path),
        "local_files_only": True,
    }
    assert row["prompt"] == "Агенты: dev. Серверы: example-prod. Проекты: digipaddock. Карточки: KD-1."
    assert row["prompt_tokens"] == 8 and model.transcribe_kwargs["initial_prompt"] == row["prompt"]
    assert model.transcribe_kwargs["vad_filter"] is True and model.transcribe_kwargs["language"] == "ru"
    assert row["text"] == "Мержи и выкати PR 17." and row["duration"] == 3.46
    assert row["versions"] == {"faster_whisper": "9.9", "ctranslate2": "4.4"} and row["peak_rss"] > 0


def test_one_without_vocab_gives_no_hint(tmp_path, fake_whisper, capsys):
    assert _one(tmp_path) == 0
    row = json.loads(capsys.readouterr().out)
    assert row["prompt"] == "" and fake_whisper.instances[0].transcribe_kwargs["initial_prompt"] is None


# --- run and summary -------------------------------------------------------------------------------------------


class FakeChild:
    def __init__(self, texts, *, fail=()):
        self.texts, self.fail, self.calls = texts, set(fail), []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        model = command[command.index("--model") + 1]
        item = Path(command[command.index("--audio") + 1]).stem
        if (model, item) in self.fail:
            return subprocess.CompletedProcess(command, 1, "", "Traceback\nRuntimeError: no model\n")
        duration = 300.0 if item.startswith("free") else 4.0
        row = {"text": self.texts.get(item, ""), "duration": duration, "load_s": 1.0, "transcribe_s": 2.0,
               "peak_rss": 900 * 1024**2 if model == "small" else 2 * 1024**3}
        return subprocess.CompletedProcess(command, 0, "loading\n" + json.dumps(row) + "\n", "")


def _items(directory: Path) -> list[dict]:
    return [{"item": item, "file": f"{item}.m4a"} for item in wp.ITEMS]


def test_run_starts_one_process_per_item_model_and_mode(tmp_path):
    child = FakeChild({})
    rows = wp.run(
        directory=tmp_path, items=_items(tmp_path), out=tmp_path / "runs", models=["small", "medium"],
        modes=wp.MODES, vocab_file=tmp_path / "vocab.json", child=["py", "-I", "probe.py"],
        models_dir=tmp_path / "m", language="ru", threads=4, beam=5, timeout=60, runner=child,
    )
    assert len(rows) == len(child.calls) == 2 * 2 * 13
    command, kwargs = child.calls[0]
    assert command[:4] == ["py", "-I", "probe.py", "one"] and "--vocab" not in command
    assert kwargs["env"]["HF_HUB_OFFLINE"] == "1" and kwargs["timeout"] == 60
    vocab_calls = [command for command, _ in child.calls if "--vocab" in command]
    assert len(vocab_calls) == 26 and vocab_calls[0][vocab_calls[0].index("--vocab") + 1] == str(tmp_path / "vocab.json")
    journal = (tmp_path / "runs" / "runs.jsonl").read_text("utf-8").splitlines()
    assert len(journal) == 52 and json.loads(journal[0])["peak_rss"] == 900 * 1024**2


def test_run_records_a_failed_child(tmp_path):
    child = FakeChild({}, fail={("medium", "p01")})
    rows = wp.run(
        directory=tmp_path, items=_items(tmp_path), out=tmp_path / "runs", models=["medium"], modes=["plain"],
        vocab_file=None, child=["py"], models_dir=tmp_path, language="ru", threads=4, beam=5, timeout=60,
        runner=child,
    )
    assert rows[0]["error"] == "RuntimeError: no model" and "text" not in rows[0]
    assert "error" not in rows[1]


def test_run_survives_a_child_without_a_result_line(tmp_path):
    def child(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "warming up\n", "")

    rows = wp.run(
        directory=tmp_path, items=_items(tmp_path)[:2], out=tmp_path / "runs", models=["small"], modes=["plain"],
        vocab_file=None, child=["py"], models_dir=tmp_path, language="ru", threads=4, beam=5, timeout=60,
        runner=child,
    )
    assert [row["error"] for row in rows] == ["no result line: warming up"] * 2


def test_run_vocab_needs_a_dictionary(tmp_path):
    with pytest.raises(wp.ProbeError, match="dictionary"):
        wp.run(
            directory=tmp_path, items=[], out=tmp_path / "runs", models=["small"], modes=wp.MODES, vocab_file=None,
            child=["py"], models_dir=tmp_path, language="ru", threads=4, beam=5, timeout=60, runner=FakeChild({}),
        )


def test_main_run_with_and_without_vocab(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(13))
    wp.collect(db, str(files), "KD-9075", tmp_path / "items")
    (tmp_path / "vocab.json").write_text(json.dumps(TERMS), "utf-8")
    child = FakeChild({})
    common = ["--items", str(tmp_path / "items"), "--models-dir", str(tmp_path / "m"), "--models", "small"]
    assert wp.main(["run", *common, "--out", str(tmp_path / "a")], runner=child, script="probe.py") == 0
    assert len(child.calls) == 13 and not any("--vocab" in command for command, _ in child.calls)
    assert child.calls[0][0][:4] == [sys.executable, "-I", "probe.py", "one"]
    assert wp.main(
        ["run", *common, "--out", str(tmp_path / "b"), "--vocab", str(tmp_path / "vocab.json")], runner=child,
        script="probe.py",
    ) == 0
    assert len(child.calls) == 13 + 26


def _rows(model, mode, texts, *, wall=200.0, peak=900 * 1024**2, skip=()):
    rows = []
    for item in wp.ITEMS:
        if item in skip:
            continue
        duration = 300.0 if item.startswith("free") else 4.0
        rows.append({"model": model, "mode": mode, "item": item, "text": texts.get(item, ""),
                     "duration": duration, "wall_s": wall if item.startswith("free") else 9.0, "peak_rss": peak})
    return rows


def test_summary_verdicts():
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    perfect = {item: wp.marked(text)[0] for item, text in phrases.items()}
    refs = {"free1": "Посмотри [KD-9041].", "free2": "Ок [dev].", "free3": "Всё."}
    perfect.update({"free1": "Посмотри KD-9041.", "free2": "Ок дев.", "free3": "Всё."})
    four_misses = dict(perfect, p01="Проверь на сервере", p03="в работу")  # disk, example-prod, the card id, dev
    rows = (
        _rows("small", "plain", perfect)
        + _rows("small", "vocab", four_misses)
        + _rows("medium", "plain", perfect, wall=400.0)
        + _rows("medium", "vocab", perfect, peak=2 * 1024**3)
    )
    table = wp.summary(rows, phrases, refs, variants)
    lines = table.splitlines()
    assert lines[2].startswith("| small | нет | 33/33 | 0 | 2/2 | 0.17 | 100% |")  # WER: "дев" for dev
    assert lines[2].endswith("| проходит |")
    assert "| small | да | 29/33 | 4 |" in lines[3] and lines[3].endswith("| не проходит |")
    # Speed and memory are reported, not judged (owner, 21.10): slower than the recording and 2 GiB still pass.
    assert "free2 300 с: 1.33" in lines[4] and lines[4].endswith("| проходит |")
    assert "| 2.00 |" in lines[5] and lines[5].endswith("| проходит |")
    assert "- small/vocab p01: нет диск, example-prod — «Проверь на сервере»" in table


def test_summary_scores_the_phrases_in_one_recording():
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    read_in_a_row = " ".join(wp.marked(text)[0] for text in phrases.values()).replace("example-prod", "сервер")
    rows = [{"model": "small", "mode": "plain", "item": "phrases", "text": read_in_a_row, "duration": 90.0,
             "wall_s": 120.0, "peak_rss": 900 * 1024**2}]
    table = wp.summary(rows, phrases, {}, variants)
    line = table.splitlines()[2]
    assert line.startswith("| small | нет | 32/33 | 1 | 0/0 | - | 97% | phrases 90 с: 1.33 |")
    assert line.endswith("| проходит (неполно) |")  # one miss; no free messages yet
    assert "- small/plain phrases: нет example-prod" in table


def test_summary_corrects_by_synonyms_first():
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    heard = " ".join(wp.marked(text)[0] for text in phrases.values())
    heard = heard.replace("example-prod", "Keychain Pro").replace("digipaddock", "Herter").replace("Сисадмин", "Тис админ")
    rows = [{"model": "medium", "mode": "vocab", "item": "phrases", "text": heard, "duration": 99.0,
             "wall_s": 80.0, "peak_rss": 2 * 1024**3}]
    assert "| 30/33 | 3 |" in wp.summary(rows, phrases, {}, variants)
    table = wp.summary(rows, phrases, {}, variants, wp.load_synonyms(SYNONYMS_FILE))
    assert "| 33/33 | 0 |" in table and "исправлены по синонимам" in table


def test_correct_replaces_whole_misheard_forms_longest_first():
    synonyms = {"example-prod": ["keychain pro"], "example": ["keychain"], "digipaddock": ["herter"]}
    assert wp.correct("На Keychain Pro, а потом Keychain-stage; Herter!", synonyms) == (
        "на example prod а потом example stage digipaddock"
    )
    assert wp.correct("Keychainы и Herters", synonyms) == "keychainы и herters"  # whole words only


@pytest.mark.parametrize(
    "data", [{"synonyms": {"digipaddock": [" "]}}, {"synonyms": {"-": ["foo"]}}, {"synonyms": []}, ["digipaddock"]]
)
def test_load_synonyms_refuses_a_bad_file(tmp_path, data):
    path = tmp_path / "s.json"
    path.write_text(json.dumps(data), "utf-8")
    with pytest.raises(wp.ProbeError, match="misheard forms"):
        wp.load_synonyms(path)


def test_the_synonyms_keep_every_right_answer():
    """Correcting by the synonyms never turns a hit into a miss: the phrases as read, and every accepted
    spelling of a keyword, still count after the correction."""
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    synonyms = wp.load_synonyms(SYNONYMS_FILE)
    for item, text in phrases.items():
        assert wp.keyword_score(text, wp.correct(wp.marked(text)[0], synonyms), variants)["misses"] == [], item
    for keyword, spellings in variants.items():
        for spelling in spellings:
            assert wp.found(keyword, wp.correct(f"а {spelling} и", synonyms), variants), (keyword, spelling)


def test_main_summary_with_synonyms(tmp_path, capsys):
    runs = tmp_path / "runs.jsonl"
    row = {"model": "small", "mode": "plain", "item": "phrases", "text": "Проект Herter", "duration": 9.0,
           "wall_s": 3.0, "peak_rss": 1}
    runs.write_text(json.dumps(row, ensure_ascii=False) + "\n", "utf-8")
    args = ["summary", str(runs), "--phrases", str(PHRASES_FILE), "--items", str(tmp_path)]
    assert wp.main([*args, "--synonyms", str(SYNONYMS_FILE)]) == 0
    out = capsys.readouterr().out
    assert "| 1/33 |" in out and "«Проект Herter»" in out  # digipaddock counted; the miss quotes what was heard


def test_summary_phrases_in_one_recording_and_free_messages_pass_complete():
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    texts = {"phrases": " ".join(wp.marked(text)[0] for text in phrases.values()),
             "free1": "Ок dev.", "free2": "Ок dev.", "free3": "Ок dev."}
    rows = [{"model": "medium", "mode": "vocab", "item": item, "text": text, "duration": 90.0, "wall_s": 60.0,
             "peak_rss": 900 * 1024**2} for item, text in texts.items()]
    table = wp.summary(rows, phrases, {item: "Ок [dev]." for item in wp.FREE}, variants)
    assert table.splitlines()[2].startswith("| medium | да | 33/33 | 0 | 3/3 | 0.00 | 100% | phrases 90 с: 0.67,")
    assert table.splitlines()[2].endswith("| проходит |")


def test_map_names_each_item_once(tmp_path):
    db, files = _cabinet(tmp_path, _recordings(2))
    with pytest.raises(wp.ProbeError, match="each item once"):
        wp.collect(db, str(files), "KD-9075", tmp_path / "a", {"Запись 0.m4a": "p01", "Запись 1.m4a": "p01"})
    assert wp._mapping(["voice.webm=phrases"]) == {"voice.webm": "phrases"}


def test_summary_marks_unscored_free_messages():
    phrases, variants = wp.load_phrases(PHRASES_FILE)
    perfect = {item: wp.marked(text)[0] for item, text in phrases.items()}
    table = wp.summary(_rows("small", "plain", perfect, skip={"free3"}), phrases, {}, variants)
    assert table.splitlines()[2].endswith("| проходит (неполно) |")
    assert "не оценены): free1, free2, free3." in table


def test_main_summary_reads_references(tmp_path, capsys):
    phrases, _ = wp.load_phrases(PHRASES_FILE)
    perfect = {item: wp.marked(text)[0] for item, text in phrases.items()}
    runs = tmp_path / "runs.jsonl"
    runs.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in _rows("small", "plain", perfect)))
    (tmp_path / "free1.ref.txt").write_text("[dev] ок", "utf-8")
    code = wp.main(["summary", str(runs), "--phrases", str(PHRASES_FILE), "--items", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0 and "| 0/1 |" in out and "free2, free3" in out


def test_main_refuses_a_bad_map(capsys):
    assert wp.main(["collect", "--card", "KD-9075", "--out", "x", "--map", "a.m4a=p11"]) == 1
    assert "expected <file name>=<item>" in capsys.readouterr().err
