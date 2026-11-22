"""The structured question form v1 (remediation spec 7.2): the form an agent opens, the answer a person
gives and the readable reply the agent and the thread get."""

import copy

import pytest

from kd.cabinet import decisions, questions
from kd.cabinet.questions import AnswerError, FormError


def _form() -> dict:
    """The spec's example (section 7.2)."""
    return {
        "schema_version": 1,
        "text": "Уточнения по проверке Hindsight",
        "questions": [
            {
                "id": "probe_method",
                "title": "Как выполнять проверку?",
                "context": "Монитор работает на том же сервере.",
                "type": "single_select",
                "required": True,
                "allow_custom": True,
                "options": [
                    {"id": "local_http", "label": "Прямой HTTP-запрос",
                     "description": "Без передачи ключа в аргументах SSH.", "recommended": True},
                    {"id": "ssh", "label": "Через SSH", "description": "Проверка выполняется через существующий шлюз."},
                ],
            },
            {
                "id": "extra_checks",
                "title": "Что дополнительно проверять?",
                "type": "multi_select",
                "required": False,
                "allow_custom": True,
                "options": [
                    {"id": "worker", "label": "Обработчик памяти", "description": "Отдельная проверка доступности."},
                    {"id": "queue", "label": "Очередь", "description": "Контроль задержки обработки."},
                ],
            },
            {"id": "details", "title": "Дополнительные условия", "type": "text", "required": False},
        ],
    }


def _answer(**items) -> dict:
    return {k: {"selected_option_ids": v[0], "custom_text": v[1]} for k, v in items.items()}


def test_a_valid_form_is_normalized_with_defaults():
    form = questions.validate_form(_form())
    assert set(form) == {"schema_version", "text", "questions"}
    details = form["questions"][2]
    assert details == {
        "id": "details", "title": "Дополнительные условия", "context": "", "type": "text",
        "required": False, "allow_custom": False, "options": [],
    }
    ssh = form["questions"][0]["options"][1]
    assert ssh == {"id": "ssh", "label": "Через SSH",
                   "description": "Проверка выполняется через существующий шлюз.", "recommended": False}
    assert questions.form_sha(form) == decisions.payload_sha(form)


def _break(change):
    form = _form()
    change(form)
    return form


def _set(path, value):
    def change(form):
        target = form
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


def _many_questions(n):
    def change(form):
        form["questions"] = [
            {"id": f"q{i}", "title": f"Вопрос {i}", "type": "text"} for i in range(n)
        ]
    return change


# fmt: off
FORM_ERRORS = [
    ("no-questions", _many_questions(0), "1 to 8 questions"),
    ("nine-questions", _many_questions(9), "1 to 8 questions"),
    ("one-option", _set(["questions", 0, "options"], [{"id": "a", "label": "А"}]), "questions[0]: 2 to 8 options"),
    ("nine-options", _set(["questions", 0, "options"], [{"id": f"o{i}", "label": str(i)} for i in range(9)]), "questions[0]: 2 to 8 options"),
    ("duplicate-question", _set(["questions", 1, "id"], "probe_method"), "duplicate question id"),
    ("duplicate-option", _set(["questions", 0, "options", 1, "id"], "local_http"), "duplicate option id"),
    ("bad-id", _set(["questions", 0, "id"], "Bad"), "questions[0].id"),
    ("bad-option-id", _set(["questions", 0, "options", 0, "id"], "1st"), "questions[0].options[0].id"),
    ("two-recommended", _set(["questions", 0, "options", 1, "recommended"], True), "at most one recommended"),
    ("text-with-options", _set(["questions", 2, "options"], [{"id": "a", "label": "А"}, {"id": "b", "label": "Б"}]), "a text question has no options"),
    ("text-allow-custom", _set(["questions", 2, "allow_custom"], True), "allow_custom"),
    ("unknown-key", _set(["questions", 0, "default"], "ssh"), "unknown key"),
    ("unknown-top-key", _set(["answers"], {}), "unknown key"),
    ("schema-version", _set(["schema_version"], 2), "schema_version"),
    ("unknown-type", _set(["questions", 2, "type"], "date"), "questions[2].type"),
    ("empty-title", _set(["questions", 0, "title"], "  "), "questions[0].title"),
    ("long-label", _set(["questions", 0, "options", 0, "label"], "x" * 121), "questions[0].options[0].label"),
    ("empty-text", _set(["text"], ""), "text"),
    ("required-not-bool", _set(["questions", 0, "required"], "yes"), "questions[0].required"),
    ("long-context", _set(["questions", 0, "context"], "x" * 2001), "questions[0].context"),
]
# fmt: on


@pytest.mark.parametrize(("change", "expected"), [pytest.param(c, e, id=i) for i, c, e in FORM_ERRORS])
def test_form_errors_name_the_place(change, expected):
    with pytest.raises(FormError) as caught:
        questions.validate_form(_break(change))
    assert expected in str(caught.value)


def test_a_form_too_large_is_refused():
    form = _form()
    form["questions"] = [  # every field within its limit, 8 × 2300 Cyrillic characters: over 32 KiB of UTF-8
        {"id": f"q{i}", "title": "т" * 300, "context": "к" * 2000, "type": "text"} for i in range(8)
    ]
    with pytest.raises(FormError) as caught:
        questions.validate_form(form)
    assert "too large" in str(caught.value)


def test_two_recommended_are_allowed_in_multi_select():
    form = _form()
    for option in form["questions"][1]["options"]:
        option["recommended"] = True
    assert all(o["recommended"] for o in questions.validate_form(form)["questions"][1]["options"])


def test_the_form_is_not_changed_in_place():
    form = _form()
    before = copy.deepcopy(form)
    questions.validate_form(form)
    assert form == before


def test_an_answer_is_normalized_in_form_order():
    form = questions.validate_form(_form())
    got = questions.validate_answer(form, _answer(probe_method=(["ssh"], "")), "")
    assert list(got) == ["probe_method", "extra_checks", "details"]
    assert got["probe_method"] == {"selected_option_ids": ["ssh"], "custom_text": ""}
    assert got["extra_checks"] == {"selected_option_ids": [], "custom_text": ""}
    assert got["details"] == {"selected_option_ids": [], "custom_text": ""}


# fmt: off
ANSWER_ERRORS = [
    ("unknown-question", {"probe_method": {"selected_option_ids": ["ssh"]}, "nope": {"custom_text": "x"}}, "", "unknown question"),
    ("unknown-option", {"probe_method": {"selected_option_ids": ["telnet"]}}, "", "unknown option"),
    ("duplicate-option", {"probe_method": {"selected_option_ids": ["ssh"]}, "extra_checks": {"selected_option_ids": ["queue", "queue"]}}, "", "duplicate"),
    ("two-for-single", {"probe_method": {"selected_option_ids": ["ssh", "local_http"]}}, "", "one option"),
    ("custom-not-allowed", None, "", "no custom answer"),
    ("ids-for-text", {"probe_method": {"selected_option_ids": ["ssh"]}, "details": {"selected_option_ids": ["ssh"]}}, "", "a text question takes text"),
    ("required-missing", {"extra_checks": {"selected_option_ids": ["queue"]}}, "", "probe_method is required"),
    ("required-blank", {"probe_method": {"selected_option_ids": [], "custom_text": "   "}}, "", "probe_method is required"),
    ("custom-too-long", {"probe_method": {"selected_option_ids": [], "custom_text": "x" * 2001}}, "", "too long"),
    ("comment-too-long", {"probe_method": {"selected_option_ids": ["ssh"]}}, "x" * 4001, "too long"),
    ("unknown-field", {"probe_method": {"selected_option_ids": ["ssh"], "why": "x"}}, "", "unknown key"),
    ("ids-not-list", {"probe_method": {"selected_option_ids": "ssh"}}, "", "selected_option_ids"),
]
# fmt: on


@pytest.mark.parametrize(("answers", "comment", "expected"), [pytest.param(a, c, e, id=i) for i, a, c, e in ANSWER_ERRORS])
def test_answer_errors(answers, comment, expected):
    raw = _form()
    if answers is None:  # custom text on a select that does not allow it
        raw["questions"][0]["allow_custom"] = False
        answers = {"probe_method": {"selected_option_ids": ["ssh"], "custom_text": "своё"}}
    form = questions.validate_form(raw)
    with pytest.raises(AnswerError) as caught:
        questions.validate_answer(form, answers, comment)
    assert expected in str(caught.value)


def test_required_is_met_by_custom_text_alone():
    form = questions.validate_form(_form())
    got = questions.validate_answer(form, _answer(probe_method=([], "Через локальный сокет")), "")
    assert got["probe_method"]["custom_text"] == "Через локальный сокет"


def test_custom_text_keeps_its_lines_and_drops_outer_space():
    form = questions.validate_form(_form())
    got = questions.validate_answer(form, _answer(probe_method=(["ssh"], "  первая\nвторая  ")), "")
    assert got["probe_method"]["custom_text"] == "первая\nвторая"


def test_readable_lists_titles_labels_and_custom():
    form = questions.validate_form(_form())
    answers = questions.validate_answer(
        form,
        _answer(probe_method=(["local_http"], ""), extra_checks=(["worker", "queue"], "и задержку")),
        "Срок — неделя",
    )
    assert questions.readable(form, answers, "Срок — неделя") == (
        "1. Как выполнять проверку?: Прямой HTTP-запрос\n"
        "2. Что дополнительно проверять?: Обработчик памяти; Очередь — и задержку\n"
        "3. Дополнительные условия: —\n\nСрок — неделя"
    )


def test_readable_drops_a_trailing_period_of_the_title_and_shows_custom_alone():
    form = questions.validate_form({
        "schema_version": 1, "text": "Раунд 1",
        "questions": [{"id": "how", "title": "Как проверять.", "type": "text"}],
    })
    answers = questions.validate_answer(form, _answer(how=([], "Напрямую")), "")
    assert questions.readable(form, answers, "") == "1. Как проверять: Напрямую"


def test_the_longest_valid_answer_fits_the_reply():
    """Eight multi-selects with the longest title, eight longest labels each, the longest own words and comment."""
    form = questions.validate_form({
        "schema_version": 1, "text": "Раунд",
        "questions": [
            {"id": f"q{i}", "title": "т" * 300, "type": "multi_select", "allow_custom": True,
             "options": [{"id": f"o{k}", "label": "м" * 120} for k in range(8)]}
            for i in range(8)
        ],
    })
    answers = questions.validate_answer(
        form, {f"q{i}": {"selected_option_ids": [f"o{k}" for k in range(8)], "custom_text": "с" * 2000}
               for i in range(8)}, "к" * 4000,
    )
    assert len(questions.readable(form, answers, "к" * 4000)) <= questions.READABLE_MAX
