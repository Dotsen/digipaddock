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


# --- the search runs under the board's one lock: its query is bounded ---------------------
def test_repeated_words_and_alternatives_are_searched_once():
    assert store.search_words("Голос голос ГОЛОС|голос|аудио голос") == [["голос"], ["голос", "аудио"]]
    assert store.search_words("а|а б") == [["а"], ["б"]]


def test_a_search_is_bounded_in_length_and_words(env):
    voice = env.card(title="Запись голоса")
    assert _ids(env, " ".join(["голос"] * 30)) == [voice.id]  # one word, however often it is repeated
    with pytest.raises(store.Invalid, match=f"at most {store.SEARCH_TEXT_MAX} characters"):
        env.store.search("г" * (store.SEARCH_TEXT_MAX + 1))
    words = [f"w{i}" for i in range(store.SEARCH_WORDS_MAX + 1)]
    for text in (" ".join(words), "|".join(words), " ".join(words[:5]) + " " + "|".join(words[5:])):
        with pytest.raises(store.Invalid, match=f"at most {store.SEARCH_WORDS_MAX} words"):
            env.store.search(text)
    assert env.store.search(" ".join(words[:-1])) == []


def test_the_api_refuses_an_unbounded_search(env):
    from kd.cabinet import api

    with pytest.raises(api.ApiError) as exc:
        env.call("root", {"op": "card.search", "text": "x" * (store.SEARCH_TEXT_MAX + 1)})
    assert exc.value.kind == "invalid" and "characters" in str(exc.value)
