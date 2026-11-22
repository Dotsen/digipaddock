"""Anonymized replicas of the 20.10 audit's two cards, for the snapshot's tests and the BOSS probe.

The audit (docs/superpowers/research/2026-10-20-pipeline-audit.md) found two cards whose threads were mostly the
agent's tool traffic: 98 KB and 68 KB, of which a reader saw the first 2 KB. Each replica rebuilds one of them in
an open ``Store`` with the same facts and about the same volume, through the store's own writes, without a host,
a domain or a person's name of the platform:

- ``kd9041_like`` — the first part (the cabinet) merged and released, the release's check failed, the second
  part (the agents' file commands) only promised in the notes, nothing queued and nobody on the card;
- ``kd9032_like`` — Blocked with no structured dependency, the owner's answer to the agent's question already
  given, the subcards asked of the coordinator in a comment and never created.
"""

from typing import Mapping

from kd.cabinet import decisions, prs_api

A, B = "KD-9041", "KD-9032"
REPO, PR = "owner/platform", 18
HEAD = "cf2d924" + "5a1e3b7c9d0f2468ace13579bdf02468ace1"
FORGE = "system:forge"
DEV = "agent:dev"
OWNER = "user:owner"

BRIEF_9041 = """# Вложения в задачах и чатах: файлы и изображения

Цель: человек и агент обмениваются файлами в треде карточки и в чате агента — картинки видны сразу, текстовые
файлы читаются агентом без копирования содержимого в сообщение.

## Часть 1 — кабинет

Загрузка файла в тред и в чат, превью изображения, удаление своего файла, пределы на файл (20 МБ) и на одно
сообщение (100 МБ), квоты на человека и на всех. Маршрут загрузок в веб-сервере принимает тело сообщения целиком.

## Часть 2 — агенты

В снимке карточки и в чате агента у каждого файла — id, тип и размер; агент читает файл командой
`kd-task file get <id>` и прикладывает свой `kd-task file add`; доступы, квоты и пути проверяет исполнитель;
сквозная проверка — картинка и текстовый файл в карточке и в чате, агент отвечает по их содержимому.

## Порядок

Два PR: сначала кабинет, потом агентская часть. Установка изменённых текстов агентов и конфигурации веб-сервера
— после merge каждого PR, с проверкой.
"""
AC_9041 = (
    ("Файл до 20 МБ загружается в тред и в чат", True),
    ("Превью изображения в треде", True),
    ("Удаление своего файла", True),
    ("Квоты на человека и на всех", True),
    ("id, тип и размер файла в снимке карточки и в чате агента", False),
    ("kd-task file get и kd-task file add", False),
    ("Доступы, квоты и пути проверяет исполнитель", False),
    ("Сквозная проверка: картинка и текст в карточке и в чате", False),
    ("Загрузка 20 МБ через веб-сервер после установки", False),
)
NOTES_9041 = (
    "[dev] spec implementer v1\n"
    "[dev] PR1 (кабинет): загрузки, превью, удаление — тесты зелёные, PR #18 опубликован.\n"
    "[dev] PR1 слит владельцем с выкаткой; проверка выкатки: установленный текст аналитика не совпал с реестром.\n"
    "[dev] Следующий шаг — PR2: id/type/size в kd-task view и chat, kd-task file get/add, сквозная проверка."
)
BRIEF_9032 = """# Голосовые сообщения в треде и в чате агента

Цель: владелец записывает голосовое в треде карточки или в чате агента, агент получает расшифровку.

Основание — вложения (KD-9041a): голосовое — файл сообщения. Сначала проба моделей расшифровки на образцах
владельца (скрипт и протокол — разработчик, запуск — на сервере исполнителя с правами), затем запись, очередь,
плеер и расшифровка в кабинете.

Этапы: «Голос 1: проба моделей и исполнитель», «Голос 2: запись, очередь, плеер и расшифровка».
"""
NOTES_9032 = "[dev] spec implementer v1\n[dev] Фразы для образцов:\n" + "\n".join(
    f"{i}. Фраза образца номер {i} для пробы расшифровки" for i in range(1, 11)
)
ANSWER_9032 = "Подзадачи создаёт BOSS, скрипт готовит dev, пробу на stage запускает sysadmin; список фраз подходит."


def _card(store, card_id: str, **values) -> None:
    now = store._clock()
    record = {
        "id": card_id, "n": int(card_id.split("-")[1]), "project": "digipaddock", "created_by": "@owner",
        "assignee": "@dev", "created": now, "updated": now, **values,
    }
    store.import_card(record, [], actor="user:owner")


def _tools(store, thread: int, session: str, command: str, pairs: int, topic: str) -> None:
    """The session's tool traffic of one long turn: a call (one line of what it ran) and its result's size."""
    for i in range(pairs):
        call = f"toolu_{topic}_{i:04d}"
        body = (
            f"Bash: python -m pytest common/tests/test_cabinet_{topic}.py -q -k case_{i:04d} — run the {topic} case "
            f"{i} against the store, then read the tail of its output for the failing assertion and the traceback "
            f"of step {i % 17}; compare it with the expected rows of the fixture before the next edit"
        )
        store.add_thread_message(thread, author=DEV, kind="tool_call", body=body,
                                 payload={"tool": "Bash", "id": call, "command": command}, session=session,
                                 via="session")
        store.add_thread_message(thread, author=DEV, kind="tool_result", body="",
                                 payload={"id": call, "ok": True, "size": 900 + i, "command": command},
                                 session=session, via="session")


def _turn(store, card_id: str, *, kind: str, text: str, key: str) -> tuple:
    """One finished turn of dev on the card: its command done, its session exited."""
    thread = store.card(card_id).thread
    session = store.session_for(thread, "dev")["id"]
    command = store.enqueue({"key": key, "agent": "dev", "thread": thread, "card": card_id, "kind": kind,
                             "text": text, "initiator": OWNER})["id"]
    for state in ("delivered", "started"):
        store.command_state(command, state, session=session)
    store.session_update(session, state="working")
    return thread, session, command


def _end(store, session: str, command: str) -> None:
    store.command_state(command, "done", session=session)
    store.session_update(session, state="exited")


def _notice(store, thread: int, key: str, params: Mapping, level: str = "info") -> None:
    store.add_thread_message(thread, author=FORGE, kind="notice", body="",
                             payload={"key": key, "params": dict(params), "level": level, "source": "forge"},
                             via="notice")


def kd9041_like(store) -> str:
    _card(store, A, title="Изображения и файлы во вложениях задач и чатов", status="In Progress",
          labels=["spec:implementer", "brief-ready"], description=BRIEF_9041 + _filler("Подробности части", 9),
          acceptance=[{"text": t, "checked": c} for t, c in AC_9041], notes=NOTES_9041, revision=19)
    ledger = decisions.Decisions(store)
    thread, session, command = _turn(store, A, kind="message", key="probe:9041:1",
                                     text=f"[board] {A}: a message for you in the thread")
    _tools(store, thread, session, command, 340, "files")
    pr = store.pr_put({"repo": REPO, "number": PR, "card": A, "project": "digipaddock", "url": "", "title": f"{A}: часть 1",
                       "head": HEAD, "state": "open", "files": [], "cage": [], "diff": "", "truncated": False,
                       "release": True}, actor="forge")
    found = ledger.open(prs_api.TYPE, card=A, payload=prs_api.payload_of(pr), requested_by=DEV, now=store._clock())
    ledger.approve(found["id"], user="owner", now=store._clock(), answer={"mode": "release", "review": "probe"},
                   event_extra={"pr": PR, "head": HEAD, "mode": "release"})
    store.pr_put({**store.pr(REPO, PR), "state": "merged"}, actor="forge")
    _notice(store, thread, "notice.forge.merged", {"pr": PR, "title": "вложения, часть 1"})
    _notice(store, thread, "notice.forge.release_degraded",
            {"host": "stage-host", "rev": HEAD[:7],
             "why": "analyst: installed playbooks/spec/task-brief.md is not the registry text (v3)"}, level="warn")
    store.add_thread_message(
        thread, author=DEV, kind="text", session=session, via="session",
        body="PR1 слит и выкачен. Установку текста аналитика и конфигурации веб-сервера сделайте, пожалуйста, сами: "
             "мне эти операции закрыты. Дальше — PR2 (агентская часть).",
        payload={"command": command},
    )
    _end(store, session, command)
    return A


def kd9032_like(store) -> str:
    _card(store, B, title="Голосовые сообщения в кабинете: запись и расшифровка для агентов", status="Blocked",
          labels=["spec:implementer", "brief-ready"], description=BRIEF_9032 + _filler("Сценарий", 5),
          acceptance=[{"text": f"Критерий голосовых {i}", "checked": False} for i in range(1, 8)], notes=NOTES_9032,
          revision=19)
    ledger = decisions.Decisions(store)
    thread, session, command = _turn(store, B, kind="assigned", key="probe:9032:1", text=f"[board] {B} assigned to you")
    _tools(store, thread, session, command, 120, "voice")
    question = ledger.open("question", card=B, requested_by=DEV, now=store._clock(),
                           payload={"text": "Кто создаёт подзадачи, кто запускает пробу на stage, подходит ли список фраз?"})
    _end(store, session, command)
    ledger.reply(question["id"], user="owner", text=ANSWER_9032, now=store._clock())
    thread, session, command = _turn(store, B, kind="answer", key="probe:9032:2",
                                     text=f"[board] {B}: your question {question['id']} has an answer")
    _tools(store, thread, session, command, 110, "voice")
    store.add_thread_message(thread, author=DEV, kind="text", body="[dev] @boss, создай, пожалуйста, подзадачи "
                             "«Голос 1: проба моделей и исполнитель» и «Голос 2: запись, очередь, плеер и расшифровка».",
                             mentions=("boss",))
    store.add_thread_message(thread, author=DEV, kind="text", session=session, via="session",
                             body="Начну скрипт пробы, когда BOSS создаст карточку «Голос 1».",
                             payload={"command": command})
    _end(store, session, command)
    return B


def _filler(title: str, sections: int) -> str:
    """Plain detail sections, so a brief has a real brief's length (about 1.2 KB each)."""
    line = ("Поведение описано по шагам: что видит человек, что получает агент, какие пределы проверяет исполнитель "
            "и что происходит при отказе. ")
    return "".join(f"\n## {title} {i}\n\n" + line * 5 + "\n" for i in range(1, sections + 1))
