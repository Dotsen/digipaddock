"""The terminal view of a card's thread (kd-cabinet card)."""

from cabinet_env import CabinetEnv

from kd.cabinet import exporter, view


def _thread(tmp_path):
    env = CabinetEnv(tmp_path)
    card = env.card()
    for kind, body, payload in (
        ("text", "Смотрю карточку", {}),
        ("tool_call", "Bash: kd-task view KD-1", {"tool": "Bash", "id": "t1"}),
        ("tool_result", "", {"id": "t1", "ok": True}),
        ("text", "Готово", {}),
    ):
        env.store.add_thread_message(
            card.thread, author="agent:analyst", kind=kind, body=body, payload=payload, via="session"
        )
    return env.store.card(card.id).to_dict(), env.store.messages(card.id)


def test_the_cli_thread_has_no_empty_lines_for_tool_results(tmp_path):
    card, messages = _thread(tmp_path)
    text = view.card_text(card, messages)
    comments = text.split("## Comments\n\n", 1)[1]
    assert "· Bash: kd-task view KD-1" in comments
    assert comments.count("@analyst ·") == 3  # two texts and the tool call; the tool result has no line
    assert "\n\n\n" not in comments and not comments.rstrip("\n").endswith("·")


def test_the_md_snapshot_has_no_entry_for_tool_results(tmp_path):
    card, messages = _thread(tmp_path)
    text = exporter.render_card(card, messages)
    assert text.count("author: ") == 3 and "[tool_result]" not in text
