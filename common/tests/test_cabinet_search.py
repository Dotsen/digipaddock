"""Board search beyond ASCII — case, ё, words, alternatives, description and notes."""

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import store


@pytest.fixture
def env(tmp_path):
    return CabinetEnv(tmp_path)


def _ids(env, text):
    return [c.id for c in env.store.search(text)]


def test_norm_folds_case_yo_and_spaces():
    assert store.norm("  Голосовые\tСООБЩЕНИЯ  ёлка ") == "голосовые сообщения елка"
    assert store.norm("ＫＤ") == "kd"  # NFKC: full-width letters
    assert store.norm(None) == ""


def test_cyrillic_any_case_and_inside_a_longer_word(env):
    voice = env.card(title="Голосовые сообщения в кабинете")
    env.card(title="Изображения и файлы")
    assert _ids(env, "голос") == [voice.id]
    assert _ids(env, "ГОЛОСОВЫЕ") == [voice.id]


def test_every_word_must_match_and_description_counts(env):
    one = env.card(title="Вложения", description="Картинки и файлы в чате агента")
    env.card(title="Вложения в треде")
    assert _ids(env, "вложения чате") == [one.id]


def test_alternatives_and_yo(env):
    voice = env.card(title="Запись голоса")
    audio = env.card(title="Ещё аудио", description="")
    assert set(_ids(env, "голос|аудио")) == {voice.id, audio.id}
    assert _ids(env, "еще") == [audio.id]


def test_title_matches_come_first(env):
    in_title = env.card(title="Уведомления push")
    in_text = env.card(title="Другое", description="про уведомления")
    assert _ids(env, "уведомления") == [in_title.id, in_text.id]


def test_an_id_first_and_empty_text(env):
    card = env.card(title="Что-то")
    assert _ids(env, card.id.lower()) == [card.id]
    assert _ids(env, "   ") == []
