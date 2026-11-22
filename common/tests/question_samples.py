"""Question samples shared by the cabinet tests: a form v1 as an agent opens it and an old text question
whose options the cabinet recognizes."""

import copy

_FORM = {
    "schema_version": 1,
    "text": "Уточнения по проверке",
    "questions": [
        {
            "id": "method",
            "title": "Как проверять?",
            "type": "single_select",
            "required": True,
            "allow_custom": True,
            "options": [
                {"id": "http", "label": "Прямой HTTP-запрос", "recommended": True},
                {"id": "ssh", "label": "Через SSH"},
            ],
        },
        {"id": "details", "title": "Что ещё учесть?", "type": "text"},
    ],
}

LEGACY = "Вопросы по ТЗ.\n\n1. Как проверять? А) напрямую; Б) через шлюз. Рекомендую А.\n2. Что ещё учесть?"


def form() -> dict:
    return copy.deepcopy(_FORM)
