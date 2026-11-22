"""kd.transcribe: one voice message on stdin → one JSON line, without the hallucinated tail, no files left."""

from __future__ import annotations

import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from kd import transcribe as tr

REPO = Path(__file__).resolve().parents[2]
SYNONYMS_FILE = REPO / "common" / "tests" / "fixtures" / "voice" / "synonyms.json"  # synthetic copy


def seg(text, start=0.0, logprob=-0.2, no_speech=0.01):
    return SimpleNamespace(text=text, start=start, avg_logprob=logprob, no_speech_prob=no_speech)


class Engine:
    """faster-whisper's surface: the recording's bytes say its length (``seconds=3.5``)."""

    def __init__(self, segments=(), speech_end=10.0, fail=None):
        self.segments, self._end, self.fail = list(segments), speech_end, fail
        self.models, self.consumed, self.tmpdir_seen = [], 0, None

    def probe_seconds(self, data):
        if data.startswith(b"declared="):  # a container that states its length
            self.decoded = False
            return float(data[9:].split(b";")[0])
        if not data.startswith(b"seconds="):
            raise ValueError("Invalid data found when processing input")
        return None  # a browser recording without a declared length

    def decode_audio(self, stream, sampling_rate):
        self.decoded = True
        data = stream.read().split(b";")[-1]
        return range(int(float(data[8:].decode()) * sampling_rate))

    def speech_end(self, samples):
        return self._end

    def WhisperModel(self, name, **kwargs):
        engine = self
        self.tmpdir_seen = tempfile.gettempdir()
        Path(self.tmpdir_seen, "scratch.bin").write_bytes(b"x")  # a library writing a temporary file
        if self.fail:
            raise self.fail

        class Model:
            hf_tokenizer = SimpleNamespace(encode=lambda text, add_special_tokens: SimpleNamespace(ids=text.split()))

            def transcribe(self, samples, **options):
                engine.options = options

                def lazily():
                    for segment in engine.segments:
                        engine.consumed += 1
                        yield segment

                return lazily(), SimpleNamespace(duration=len(samples) / tr.SAMPLE_RATE)

        self.models.append((name, kwargs))
        return Model()


def ask(engine, *, header=None, audio=b"seconds=4", synonyms=None, max_seconds=300):
    header = {"language": "ru"} if header is None else header
    stdin = io.BytesIO(json.dumps(header, ensure_ascii=False).encode() + b"\n" + audio)
    stdout = io.StringIO()
    code = tr.serve(
        stdin, stdout, model="medium", models_dir="/models", threads=4, beam=5,
        synonyms_path=str(synonyms) if synonyms else None, max_seconds=max_seconds, engine_factory=lambda: engine,
    )
    lines = stdout.getvalue().splitlines()
    assert code == 0 and len(lines) == 1  # one JSON line, nothing else
    return json.loads(lines[0])


# --- the answer --------------------------------------------------------------------------------------------------


def test_a_deployment_without_its_synonyms_file_transcribes_as_heard(tmp_path):
    """The vocabulary comes from the overlay; a deployment without one gets the plain transcript."""
    answer = ask(Engine([seg("Проект Herter.")]), synonyms=tmp_path / "missing.json")
    assert answer["status"] == "ok" and answer["text"] == "Проект Herter."


def test_ok_with_synonyms_keeping_the_rest_of_the_text():
    engine = Engine([seg("Проект Herter, карточку KN190 в ревью."), seg(" Мерджи PR номер 17.", start=4.0)])
    answer = ask(engine, synonyms=SYNONYMS_FILE)
    assert answer["status"] == "ok"
    assert answer["text"] == "Проект digipaddock, карточку KN190 в Review. Мержи PR номер 17."
    assert answer["duration"] == 4.0 and answer["model"] == "medium" and answer["seconds"] >= 0
    assert set(answer) == {"status", "text", "duration", "seconds", "model"}


def test_a_probe_loads_the_model_and_decodes_even_silence():
    engine = Engine([seg("")], speech_end=None)
    answer = ask(engine, header={"language": "ru", "probe": True}, audio=b"seconds=1")
    assert answer["status"] == "probe_ok" and answer["text"] == ""
    assert engine.models and engine.options["vad_filter"] is False and engine.consumed == 1


def test_a_probe_of_a_real_message_length_is_refused():
    engine = Engine([seg("Привет.")])
    answer = ask(engine, header={"language": "ru", "probe": True}, audio=b"seconds=60")
    assert answer["status"] == "bad_request" and engine.models == []


def test_probe_is_a_boolean():
    with pytest.raises(tr.Rejected):
        tr.read_request(io.BytesIO(b'{"probe": "yes"}\nseconds=1'))


def test_the_model_is_loaded_offline_and_asked_not_to_continue_its_own_text():
    engine = Engine([seg("Привет.")])
    ask(engine, header={"language": "en", "vocab": {"agents": ["dev", "boss"], "cards": ["KD-1", "KD-2"]}})
    assert engine.models == [("medium", {"device": "cpu", "compute_type": "int8", "cpu_threads": 4,
                                         "download_root": "/models", "local_files_only": True})]
    options = engine.options
    assert options["language"] == "en" and options["beam_size"] == 5 and options["vad_filter"] is True
    assert options["condition_on_previous_text"] is False and options["hallucination_silence_threshold"] == 2.0
    assert options["initial_prompt"] == "Агенты: dev, boss. Карточки: KD-1, KD-2."


def test_the_hint_keeps_within_the_prompt_tokens(monkeypatch):
    monkeypatch.setattr(tr, "PROMPT_TOKENS", 4)
    engine = Engine([seg("Привет.")])
    ask(engine, header={"vocab": {"agents": ["dev"], "cards": ["KD-1", "KD-2"]}})
    assert engine.options["initial_prompt"] == "Агенты: dev. Карточки: KD-1."  # 4 words: whole terms only


def test_the_hallucinated_tail_is_cut():
    engine = Engine([  # the tail of large-v3-turbo with the dictionary, 21.10
        seg("13. В расходе токенов непонятно, по какой цене считается расход.", start=80.0),
        seg("12. ποια сад leftу говорит о куча measures", start=86.0),  # another script: the tail begins
        seg("до встречи.", start=88.0),
        seg("16. КОнешные крсенка коулаки, 다у кажется", start=90.0),  # the second: everything from the first goes
        seg("Кentre ютак шп posture приходил", start=93.0),
    ], speech_end=99.0)
    answer = ask(engine)
    assert answer["text"] == "13. В расходе токенов непонятно, по какой цене считается расход."
    assert engine.consumed == 4  # the decoding stops at the second garbled segment


def test_garbled_segments_far_apart_are_not_one_tail():
    engine = Engine([seg("β-версия готова.", start=20.0), seg("Проверь её.", start=25.0),
                     seg("다у", start=290.0), seg("ποια", start=292.0)], speech_end=299.0)
    assert ask(engine)["text"] == "Проверь её."  # the speech between them stays; the tail at 4:50 goes


def test_one_garbled_segment_inside_speech_is_dropped_alone():
    engine = Engine([seg("Закрой все PRы и commitы.", start=0.0), seg("ποια", start=3.0),
                     seg("Потом выкати.", start=5.0)])
    assert ask(engine)["text"] == "Закрой все PRы и commitы. Потом выкати."


def test_a_single_doubtful_segment_is_empty():
    assert ask(Engine([seg("Спасибо.", logprob=-1.3)]))["status"] == "empty"


def test_nothing_after_the_last_speech_and_no_doubtful_end():
    engine = Engine(
        [seg("Проверь диск.", start=0.0), seg("Тишина.", start=2.0, logprob=-1.5, no_speech=0.9),
         seg("И напиши.", start=3.0), seg("Спасибо.", start=4.2, logprob=-1.4), seg("До встречи.", start=9.0)],
        speech_end=5.0,
    )
    assert ask(engine)["text"] == "Проверь диск. И напиши."


def test_silence_is_empty_without_loading_a_model():
    engine = Engine(speech_end=None)
    assert ask(engine)["status"] == "empty" and engine.models == []


def test_nothing_left_after_the_filters_is_empty():
    answer = ask(Engine([seg("ποια measures")]))
    assert answer["status"] == "empty" and answer["text"] == ""


@pytest.mark.parametrize(
    "audio, status", [(b"seconds=301", "too_long"), (b"\x1aE\xdf\xa3 not a recording", "not_audio"), (b"", "not_audio")]
)
def test_recordings_the_executor_refuses(audio, status):
    engine = Engine([seg("Привет.")])
    answer = ask(engine, audio=audio)
    assert answer["status"] == status and answer["text"] == "" and engine.models == []
    assert answer["duration"] is None


def test_a_declared_length_refuses_before_decoding():
    engine = Engine([seg("Привет.")])
    answer = ask(engine, audio=b"declared=4200;seconds=4200")
    assert answer["status"] == "too_long" and engine.decoded is False


def test_too_large():
    stdin = io.BytesIO(b'{"language": "ru"}\n' + b"seconds=4" * 3)
    with pytest.raises(tr.Rejected) as caught:
        tr.read_request(stdin, audio_max=10)
    assert caught.value.status == "too_large"


@pytest.mark.parametrize(
    "line",
    [b"not json\n", b"[1]\n", b'{"language": "de"}\n', b'{"vocab": {"secrets": ["x"]}}\n',
     b'{"vocab": {"agents": [1]}}\n', b'{"language": "ru"}'],
)
def test_bad_requests(line):
    with pytest.raises(tr.Rejected) as caught:
        tr.read_request(io.BytesIO(line + b"seconds=4" if line.endswith(b"\n") else line))
    assert caught.value.status == "bad_request"


def test_a_header_over_the_limit_is_refused():
    with pytest.raises(tr.Rejected) as caught:
        tr.read_request(io.BytesIO(b"{" + b" " * 100 + b"}\nseconds=4"), header_max=50)
    assert caught.value.status == "bad_request"


def test_a_failing_model_answers_error():
    engine = Engine([seg("Привет.")], fail=RuntimeError("model.bin is missing"))
    assert ask(engine)["status"] == "error"


# --- no files left -------------------------------------------------------------------------------------------------


def test_the_temporary_folder_is_removed_and_tmpdir_restored(monkeypatch, tmp_path):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    before = tempfile.tempdir
    for engine in (Engine([seg("Привет.")]), Engine([seg("Привет.")], fail=RuntimeError("boom"))):
        ask(engine)
        assert engine.tmpdir_seen and Path(engine.tmpdir_seen).name.startswith("kd-transcribe-")
        assert not Path(engine.tmpdir_seen).exists()  # with the file the library wrote in it
        assert os.environ["TMPDIR"] == str(tmp_path) and tempfile.tempdir == before
    assert list(tmp_path.iterdir()) == []


def test_the_answer_carries_only_text_and_measures():
    answer = ask(Engine([seg("Привет.")]), header={"language": "ru", "vocab": {"agents": ["dev"]}})
    assert "Агенты" not in json.dumps(answer, ensure_ascii=False)  # the hint stays inside


# --- text helpers --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Проверь disk 502.", "Сделай бэкап базы на app-бэкап.", "KD кабинет, PR №17", "...", "Ünïcödé café",
     "закрой все PRы и commitы", "м² за ½ часа", "Сalibri"],  # mixed words are this team's speech
)
def test_ordinary_text_is_not_garbled(text):
    assert not tr.garbled(text)


@pytest.mark.parametrize("text", ["12. ποια сад", "16. КОнешные крсенка коулаки, 다у кажется"])
def test_the_tail_of_21_10_is_garbled(text):
    assert tr.garbled(text)


def test_apply_synonyms():
    synonyms = {"example-prod": ["keychain pro"], "example": ["keychain"], "Сисадмин": ["тис админ"],
                "git diff": ["gitdiv"], "дев": ["девэ"]}
    assert tr.apply_synonyms("На Keychain Pro, а потом Keychain-stage.", synonyms) == (
        "На example-prod, а потом example-stage."
    )
    assert tr.apply_synonyms("Тис-админ, покажи GitDiv!", synonyms) == "Сисадмин, покажи git diff!"
    assert tr.apply_synonyms("Keychains и Тисадминка", synonyms) == "Keychains и Тисадминка"  # whole words only
    assert tr.apply_synonyms("девё", synonyms) == "дев"  # е, ё and э are one letter
    phrases = {"testapi": ["тест api"], "pytest": ["по тест"]}
    assert tr.apply_synonyms("Запусти тест. API ноды; по тест-кейсам", phrases) == "Запусти тест. API ноды; pytest-кейсам"


def test_main_keeps_library_prints_out_of_the_answer(monkeypatch, capfd):
    class Noisy(Engine):
        def WhisperModel(self, name, **kwargs):
            print("ctranslate2: loading model")  # a library writing to fd 1
            os.write(1, b"native noise\n")
            return super().WhisperModel(name, **kwargs)

    monkeypatch.setattr("sys.stdin", SimpleNamespace(buffer=io.BytesIO(b'{"language": "ru"}\nseconds=2')))
    saved = os.dup(1)
    try:
        assert tr.main(["--model", "small", "--models-dir", "/m"], engine_factory=lambda: Noisy([seg("Ок.")])) == 0
    finally:
        os.dup2(saved, 1)
        os.close(saved)
    out, err = capfd.readouterr()
    assert [json.loads(line)["text"] for line in out.splitlines()] == ["Ок."]
    assert "ctranslate2: loading model" in err and "native noise" in err


def test_main_reads_stdin_and_writes_one_line(monkeypatch, capsys):
    stdin = SimpleNamespace(buffer=io.BytesIO(b'{"language": "ru"}\nseconds=2'))
    monkeypatch.setattr("sys.stdin", stdin)
    code = tr.main(
        ["--model", "small", "--models-dir", "/m"], engine_factory=lambda: Engine([seg("Ок.")]), guard_stdout=False
    )
    out = capsys.readouterr().out.splitlines()
    assert code == 0 and json.loads(out[0])["text"] == "Ок." and len(out) == 1
