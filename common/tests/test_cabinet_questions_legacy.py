"""Old text questions read as forms: only numbered questions and А/Б (Cyrillic), A/B, 1/2
options that are unambiguous; anything else stays text, nothing is cut or reworded."""

from kd.cabinet.questions_legacy import recognize

KD39_LIKE = (
    "Вопросы по ТЗ (прочитал монитор и установку сервиса памяти).\n\n"
    "Сегодня: монитор тикает каждые 5 мин; API памяти слушает локальный порт.\n\n"
    "1. Как проверять. А) прямой HTTP-запрос из монитора на локальный порт (монитор и так живёт на том же "
    "хосте); Б) через шлюз командой curl — тогда ключ шёл бы в аргументах команды. Рекомендую А.\n\n"
    "2. Откуда ключ, если /health под ключом. А) из уже существующего файла настроек пользователя монитора "
    "(0600, новых копий секрета нет); Б) отдельный файл только для монитора (например /etc/example/monitor.key, "
    "root:digi 0440), раскладывает установщик монитора. Рекомендую А; если /health без ключа — ключ не трогаем "
    "вовсе.\n\n"
    "3. Серьёзность проблемы «сервис не отвечает» (2 тика подряд): warn или crit? Рекомендую warn: агенты "
    "работают без памяти, но работают.\n\n"
    "4. Проверять ли и обработчик той же проверкой? Рекомендую да, отдельной проблемой.\n\n"
    "5. Проба остановкой API — оставить с вашим отдельным «да» на тихое окно? Рекомендую да; длительность ~12 мин."
)


def test_a_kd39_like_round_keeps_two_choices_and_three_texts():
    form = recognize(KD39_LIKE)
    assert form["schema_version"] == 1
    assert [q["id"] for q in form["questions"]] == ["q1", "q2", "q3", "q4", "q5"]
    assert [q["type"] for q in form["questions"]] == ["single_select", "single_select", "text", "text", "text"]
    first, second, third = form["questions"][:3]
    assert first["title"] == "Как проверять."
    assert [o["label"] for o in first["options"]] == ["А", "Б"]
    assert [o["id"] for o in first["options"]] == ["o1", "o2"]
    assert [o["recommended"] for o in first["options"]] == [True, False]
    assert first["note"] == "Рекомендую А."
    assert first["allow_custom"] is True and first["required"] is False
    assert second["note"] == "Рекомендую А; если /health без ключа — ключ не трогаем вовсе."
    assert [o["recommended"] for o in second["options"]] == [True, False]
    assert third["title"] == "Серьёзность проблемы «сервис не отвечает» (2 тика подряд): warn или crit?"
    assert third["context"] == "Рекомендую warn: агенты работают без памяти, но работают."
    assert third["options"] == [] and "note" not in third
    assert form["text"] == (
        "Вопросы по ТЗ (прочитал монитор и установку сервиса памяти).\n\n"
        "Сегодня: монитор тикает каждые 5 мин; API памяти слушает локальный порт."
    )


def test_options_keep_their_full_text():
    second = recognize(KD39_LIKE)["questions"][1]
    assert [o["description"] for o in second["options"]] == [
        "из уже существующего файла настроек пользователя монитора (0600, новых копий секрета нет)",
        "отдельный файл только для монитора (например /etc/example/monitor.key, root:digi 0440), раскладывает "
        "установщик монитора.",
    ]


def test_markers_inside_quotes_code_and_brackets_are_text():
    form = recognize(
        "1. Что выбрать? Пишите «А) нет» или `Б) нет` (вариант 1) нет).\n"
        "2. Как назвать? А) коротко; Б) полно."
    )
    assert [q["type"] for q in form["questions"]] == ["text", "single_select"]
    assert form["questions"][0]["context"] == "Пишите «А) нет» или `Б) нет` (вариант 1) нет)."


def test_a_sentence_end_inside_brackets_does_not_end_the_last_option():
    form = recognize("1. Где? А) здесь; Б) там (см. раздел 3) и дальше. Рекомендую Б.\n2. Когда?")
    options = form["questions"][0]["options"]
    assert options[1]["description"] == "там (см. раздел 3) и дальше."
    assert form["questions"][0]["note"] == "Рекомендую Б."


def test_a_broken_sequence_is_text():
    form = recognize("1. Как? А) один; В) два.\n2. Что? А) раз; Б) два; А) три.")
    assert [q["type"] for q in form["questions"]] == ["text", "text"]
    assert form["questions"][1]["context"] == "А) раз; Б) два; А) три."


def test_latin_and_digit_markers():
    form = recognize("1. Какой? A) первый; B) второй. Рекомендация: B.\n2. Сколько? 1) один; 2) два; 3) три.")
    first, second = form["questions"]
    assert [o["label"] for o in first["options"]] == ["A", "B"]
    assert [o["recommended"] for o in first["options"]] == [False, True]
    assert [o["label"] for o in second["options"]] == ["1", "2", "3"]
    assert second["options"][2]["description"] == "три."


def test_a_summary_without_questions_is_not_a_form():
    assert recognize("Понял так:\n\n1. Проверка — каждые 5 минут.\n2. Ключ — из файла.\n3. Размер S. Верно?") is None
    # the analyst's summary today
    assert recognize("Цель: проверка доступности.\nЧто делаем:\n1. Проверка — каждые 5 минут.\n2. Ключ — из файла.") is None


def test_one_question_without_options_is_not_a_form():
    assert recognize("1. Делаем так?") is None
    assert recognize("Понял так: проверка каждые 5 минут. Верно?") is None


def test_numbering_must_start_at_one_and_run():
    assert recognize("2. Что? А) да; Б) нет.\n3. Где?") is None
    assert recognize("1. Что? А) да; Б) нет.\n3. Где?") is None
    assert recognize("1. Что? А) да; Б) нет.\n1. Где?") is None


def test_more_than_eight_items_is_not_a_form():
    text = "\n".join(f"{n}. Вопрос {n}?" for n in range(1, 10))
    assert recognize(text) is None


def test_a_multiline_item_keeps_its_lines():
    form = recognize("1. Как назвать?\nСейчас — по-старому.\nА) коротко; Б) полно.\n2. Где?")
    first = form["questions"][0]
    assert first["title"] == "Как назвать?"
    assert first["context"] == "Сейчас — по-старому."
    assert [o["description"] for o in first["options"]] == ["коротко", "полно."]


def test_recommendation_must_name_a_marker():
    form = recognize("1. Что? А) да; Б) нет. Рекомендую warn.\n2. Где? А) тут; Б) там. Рекомендую Аналог.")
    for question in form["questions"]:
        assert not any(o["recommended"] for o in question["options"])


def test_empty_or_odd_input_is_not_a_form():
    for text in ("", "   ", "1.", "1. ", "А) да; Б) нет."):
        assert recognize(text) is None


def test_a_marker_in_mid_phrase_is_not_an_option():
    """In "at once or B)" the B) follows a word, not a line break or a punctuation mark."""
    text = "Вопросы.\n\n1. Как лучше: А) сразу или Б) после окна?\n\n2. Кому показывать. А) владельцу; Б) всем."
    form = recognize(text)
    first, second = form["questions"]
    assert (first["type"], first["options"]) == ("text", [])
    assert "А) сразу или Б) после окна?" in first["title"] + first.get("context", "")
    assert second["type"] == "single_select"
    assert [o["description"] for o in second["options"]] == ["владельцу", "всем."]
    lines = recognize("1. Где хранить?\nА) в базе\nБ) в файлах\n\n2. Когда?")
    assert [o["description"] for o in lines["questions"][0]["options"]] == ["в базе", "в файлах"]
