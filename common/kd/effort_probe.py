"""The analyst's effort probe: the final check of a brief, medium against high, on fixed snapshots.

Two steps on the stage host, both read-only for the board:

``extract`` (root) writes the snapshots: a brief version the analyst handed over (``brief_versions`` of the
cabinet base, opened read-only) or a draft as the analyst wrote it (the ``kd-task edit … --description`` of its
own session transcript, matched by card and revision), each as ``<card>.md`` with ``snapshots.json`` (source,
sha256), and the repository at the snapshots' commit (``--repo``, unpacked under ``repo/<name>``, ``repos.json``):
the live copies have moved on and hold the probed cards' implementations. The owner's later remarks are not part
of any snapshot: they stay with the person who grades, on the control card the probe never opens (``--hide``).

``run`` (as the agent's own user) starts one headless Claude Code session per card, effort and repeat with the
agent's compiled settings (``kd.launch``: the same checks as agent-launch), the exact model and ``--effort``. A
probe writes nothing and sees no remark: its settings deny every board write, ``kd-work``, Edit/Write, the probed
cards (their threads hold the remarks) and the hidden control cards; the Read rules and the sandbox close the
agent's notes, its old sessions, the board's md mirror and, with a pinned repository, the live copies and kd-repo; ``$HINDSIGHT_CONFIG`` turns the memory hooks off and ``--strict-mcp-config``
leaves no MCP server. A run that still saw something (``leaks``) is discarded and repeated. Each run leaves the raw
stream, the answer and a line in ``runs.jsonl`` (time, turns, tokens, cost, leaks); ``summary`` prints the table
per card and effort. Grading the answers against the known remarks is the developer's, by hand.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import tarfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from statistics import median
from typing import Any

from kd import agents, claude_code, ids, launch, naming
from kd.manifest import EFFORTS, check_model

CABINET_DB = "/var/lib/digipaddock/cabinet/cabinet.db"
CARD = ids.CARD
# What a probe session must not do or read (Claude Code permission rules, deny wins over any allow).
WRITE_DENY = (
    "Agent",  # a subagent may run another model, and its calls are not in the run's stream (leaks)
    "Edit",
    "Write",
    "NotebookEdit",
    "mcp__hindsight",
    "Bash(kd-task edit *)",
    "Bash(kd-task handoff *)",
    "Bash(kd-task create *)",
    "Bash(kd-task claim *)",
    "Bash(kd-task chat*)",  # the agent's chat with the owner may have discussed the briefs
    "Bash(kd-work *)",
)
# Hindsight reaches a session through the user's own hooks (~/.claude/settings.json: SessionStart and
# UserPromptSubmit inject the bank's pages and memories) and the user-scope MCP server. A probe points the hooks at
# this config through $HINDSIGHT_CONFIG — no path is opted in, nothing is injected or retained — and starts with
# --strict-mcp-config, so no MCP server at all.
MEMORY_OFF = {
    "serverMode": "self-hosted",
    "apiUrl": "http://127.0.0.1:9077",
    "optInOnly": True,
    "optInPaths": [],
    "mapPathToBank": {},
    "autoInject": "none",
    "retainSessions": False,
    "autoSeed": False,
    "gitIngest": "none",
    "codebaseSurvey": False,
    "autoUpdate": False,
}
BOARD_MIRROR = "/srv/digipaddock-agents/board"  # the nightly md copy of the board: the cards' threads too
REPOS_ROOT = "/srv/digipaddock-repos"  # the live read-only repository copies (kd.repo_read)
# Injected memory anywhere in a run's stream: the run saw what the snapshot must not show.
INJECTED = ("<hindsight_knowledge", "<hindsight_memory")


class ProbeError(ValueError):
    """A probe that must not run or a snapshot that cannot be taken."""


# --- extract ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    card: str
    source: str  # "brief v1" or "draft r5"
    description: str
    acceptance: tuple[str, ...]

    def text(self) -> str:
        criteria = "\n".join(f"- {item}" for item in self.acceptance) or "(критериев приёмки ещё нет)"
        return f"{self.description.rstrip()}\n\n## Acceptance Criteria\n\n{criteria}\n"


def _card(value: str) -> str:
    if not CARD.fullmatch(value):
        raise ProbeError(f"not a card id: {value}")
    return value


def _pair(value: str) -> tuple[str, int]:
    card, _, number = value.partition(":")
    if not number.isdigit():
        raise ProbeError(f"expected KD-<n>:<number>, got {value}")
    return _card(card), int(number)


def brief_version(db: str, card: str, version: int) -> Snapshot:
    """A brief version as the cabinet recorded it at the analyst's handoff (read-only open, no migration)."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT description, acceptance FROM brief_versions WHERE card=? AND version=?", (card, version)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise ProbeError(f"{card}: no brief version {version}")
    return Snapshot(card, f"brief v{version}", row[0], tuple(json.loads(row[1])))


def _commands(transcript: Path) -> Iterable[str]:
    """Bash commands of one Claude Code session transcript, in order."""
    for line in transcript.read_text("utf-8", "replace").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        content = (entry.get("message") or {}).get("content") if isinstance(entry, dict) else None
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") == "Bash":
                command = (block.get("input") or {}).get("command")
                if isinstance(command, str):
                    yield command


def draft(transcripts: Path, card: str, revision: int) -> Snapshot:
    """The description the agent wrote with ``kd-task edit <card> --revision <revision> --description …``
    (the draft before a handoff: the cabinet keeps no text of it). Criteria added later are not part of it."""
    found = []
    for path in sorted(transcripts.glob("*.jsonl")):
        for command in _commands(path):
            try:
                words = shlex.split(command)
            except ValueError:
                continue
            head = [naming.canonical_command(words[0]), *words[1:3]] if words else []
            if head != ["kd-task", "edit", card] or _value(words, "--revision") != str(revision):
                continue
            description = _value(words, "--description")
            if description is not None:
                criteria = tuple(words[i + 1] for i, word in enumerate(words[:-1]) if word == "--ac")
                found.append(Snapshot(card, f"draft r{revision}", description, criteria))
    if len(set(found)) != 1:
        raise ProbeError(f"{card}: {len(set(found))} drafts at revision {revision}, need exactly one")
    return found[0]


def _value(words: list[str], option: str) -> str | None:
    if option in words[:-1]:
        return words[words.index(option) + 1]
    return None


def pin_repo(out: Path, name: str, source: str, rev: str, git: Callable[..., bytes] | None = None) -> str:
    """The repository copy ``source`` as it was at ``rev``, unpacked into ``out/repo/<name>``: the code the briefs
    were written against (the live copies have moved on, the probed cards are implemented). Returns the commit."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", name) or not re.fullmatch(r"[0-9a-f]{7,40}", rev):
        raise ProbeError(f"expected <name>=<path>@<sha>, got {name}={source}@{rev}")
    source = os.path.realpath(source)  # safe.directory matches the exact path
    run_git = git or (lambda *args: subprocess.run(
        ["git", "-c", f"safe.directory={source}", "-C", source, *args], check=True, capture_output=True
    ).stdout)
    try:
        commit = run_git("rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
        archive = run_git("archive", "--format=tar", commit)
    except subprocess.CalledProcessError as exc:
        raise ProbeError(f"{source}: no commit {rev}: {exc.stderr.decode(errors='replace').strip()}") from None
    target = out / "repo" / name
    target.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            tar.extractall(target, filter=_no_instructions)
    except tarfile.TarError as exc:
        raise ProbeError(f"{source}@{commit}: {exc}") from None
    return commit


def _no_instructions(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """tarfile's data filter, minus the repository's CLAUDE.md files: the copy lies under the probe's cwd, where
    Claude Code would load them as instructions (the live copy outside the cwd never was)."""
    if PurePosixPath(member.name).name in ("CLAUDE.md", "CLAUDE.local.md"):
        return None
    return tarfile.data_filter(member, path)


def _repo_spec(value: str) -> tuple[str, str, str]:
    name, _, rest = value.partition("=")
    source, _, rev = rest.rpartition("@")
    if not name or not source or not rev:
        raise ProbeError(f"expected <name>=<path>@<sha>, got {value}")
    return name, source, rev


def write_snapshots(
    out: Path, snapshots: Sequence[Snapshot], repos: Mapping[str, str] | None = None
) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    index = {}
    for item in snapshots:
        data = item.text().encode("utf-8")
        (out / f"{item.card}.md").write_bytes(data)
        index[item.card] = {"source": item.source, "sha256": hashlib.sha256(data).hexdigest()}
    (out / "snapshots.json").write_text(json.dumps(index, indent=2, ensure_ascii=False) + "\n", "utf-8")
    pinned = {name: {"commit": commit, "path": f"repo/{name}"} for name, commit in (repos or {}).items()}
    (out / "repos.json").write_text(json.dumps(pinned, indent=2) + "\n", "utf-8")
    return {"snapshots": index, "repos": pinned}


# --- run -------------------------------------------------------------------------------------------------------


PROMPT = """This is a probe of your final check (playbooks/spec/task-brief.md, «Final check»), not real work.

Below is the brief of {card} exactly as it stood at {source}. The card and its thread have moved on since: do not \
open {cards} on the board, do not read memory/notes.md and do not use memory tools — work only from this snapshot, \
the repository as it was at that moment, the docs and other board cards. Write nothing anywhere: no kd-task edit, \
handoff or create, no kd-work, no files.
{repos}
Run the final check on the snapshot as if you were about to hand it over, and answer in Russian with:
1. A numbered list of findings. Each: severity (блокирует / важно / мелочь), the point of the check, the place in \
the brief, the evidence (file:line, card or doc), the fix you would make.
2. The goal points that have no observable acceptance criterion.
3. The questions you would still put to the owner, if any, and why each is his to decide.

=== Brief {card} ({source}) ===
{text}
{context}"""


def prompt(
    card: str,
    snapshots: Mapping[str, tuple[str, str]],
    hidden: Iterable[str] = (),
    repos: Mapping[str, tuple[str, str]] | None = None,
) -> str:
    """The same frame for every run: the probed snapshot, then the others as the neighbours of that moment."""
    source, text = snapshots[card]
    others = [
        f"=== Neighbouring card {other} ({snapshots[other][0]}), for context ===\n{snapshots[other][1]}"
        for other in sorted(snapshots)
        if other != card
    ]
    pinned = "".join(
        f"\nThe repository {name} as it was at that moment (commit {commit[:12]}) is at {path}: read the code there."
        for name, (commit, path) in sorted((repos or {}).items())
    )
    if pinned:
        pinned += f" {REPOS_ROOT} and kd-repo show a later state and are closed to this probe.\n"
    return PROMPT.format(
        card=card, source=source, cards=", ".join(sorted({*snapshots, *hidden})), text=text,
        context="\n".join(others), repos=pinned,
    )


def closed_paths(agent_folder: str, home: str, pinned: bool = False, out: str | None = None) -> list[str]:
    """What a probe must not read: the agent's notes, its old sessions (their tool outputs hold the cards' threads),
    the board's md mirror (the threads too), the probe's own output (the answers of the runs before it) and, with
    the repository pinned at the snapshots' commit, the live copies (the probed cards are implemented there)."""
    paths = [f"{agent_folder}/memory", f"{home}/.claude/projects", BOARD_MIRROR]
    if out:
        paths.append(out)
    return [*paths, REPOS_ROOT] if pinned else paths


def probe_settings(
    settings: str,
    cards: Iterable[str],
    agent_folder: str,
    home: str,
    pinned: bool = False,
    out: str | None = None,
) -> str:
    """The agent's merged settings with the probe's denies added (deny wins over every allow). ``cards`` — every
    card the probe must not open: the probed ones and the ones that hold the known remarks."""
    data = json.loads(settings)
    permissions = data.setdefault("permissions", {})
    deny = list(permissions.get("deny", []))
    deny += WRITE_DENY
    deny += [f"Bash(kd-task view {card}*)" for card in sorted(cards)]
    if pinned:
        deny.append("Bash(kd-repo *)")
    closed = closed_paths(agent_folder, home, pinned, out)
    deny += [f"Read(/{path}/**)" for path in closed]
    permissions["deny"] = sorted(set(deny))
    # Bash reads past the Read rules: the sandbox closes the same paths (and stops re-opening the old sessions).
    filesystem = data.setdefault("sandbox", {}).setdefault("filesystem", {})
    filesystem["allowRead"] = sorted(
        path for path in filesystem.get("allowRead", []) if not any(_within(path, item) for item in closed)
    )
    filesystem["denyRead"] = sorted(set(filesystem.get("denyRead", [])) | set(closed))
    return json.dumps(data, sort_keys=True, separators=(",", ":"))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def argv(settings: str, folder: str, model: str, effort: str) -> list[str]:
    return [
        claude_code.BINARY, "--settings", settings, "--add-dir", folder, "--model", model, "--effort", effort,
        "--strict-mcp-config", "-p", "--output-format", "stream-json", "--verbose",
    ]


def _blocks(event: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    content = (event.get("message") or {}).get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def _unquoted(text: str) -> str:
    """A tool input without shell quotes, so `kd-task view "KD-<n>"` reads as `kd-task view KD-<n>`."""
    return re.sub(r"""\\?["']""", "", text)


def leaks(
    lines: Sequence[str], cards: Iterable[str], closed: Iterable[str], pinned: bool = False
) -> list[str]:
    """What a run saw that the snapshot must not show: injected memory, or a tool call on a closed path, the
    notes file, a closed card (probed or holding the remarks) or, pinned, kd-repo that was not refused. A run with
    any is discarded and repeated."""
    task = naming.command("kd-task")
    marks = [*closed, "memory/notes.md", f"{task} chat", *(f"{task} view {card}" for card in sorted(cards))]
    if pinned:
        marks.append(naming.command("kd-repo") + " ")
    found = [mark for mark in INJECTED if any(mark in line for line in lines)]
    calls: dict[str, list[str]] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        for block in _blocks(event):
            if block.get("type") == "tool_use":
                text = _unquoted(json.dumps(block.get("input"), ensure_ascii=False))
                hit = [mark for mark in marks if re.search(re.escape(mark) + r"(?![0-9])", text)]
                if hit:
                    calls[str(block.get("id"))] = hit
            elif block.get("type") == "tool_result" and not block.get("is_error"):
                found += calls.pop(str(block.get("tool_use_id")), [])
    return sorted(set(found))


def schedule(cards: Sequence[str], efforts: Sequence[str], repeats: int) -> list[tuple[int, str, str]]:
    """Interleaved order: each repeat walks the cards, the effort that goes first alternates, so drift in the
    service over the probe's hours falls on both efforts alike."""
    order = []
    for repeat in range(1, repeats + 1):
        for index, card in enumerate(cards):
            turn = list(efforts) if (repeat + index) % 2 else list(reversed(efforts))
            order += [(repeat, card, effort) for effort in turn]
    return order


def outcome(lines: Iterable[str]) -> dict[str, Any]:
    """The init and result events of a stream-json session: model, version, time, turns, tokens, answer."""
    init, result = {}, {}
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            init = event
        elif event.get("type") == "result":
            result = event
    usage = result.get("usage") or {}
    return {
        "model": init.get("model"),
        "claude_code_version": init.get("claude_code_version"),
        "session_id": result.get("session_id") or init.get("session_id"),
        "is_error": bool(result.get("is_error", not result)),
        "duration_ms": result.get("duration_ms"),
        "num_turns": result.get("num_turns"),
        "total_cost_usd": result.get("total_cost_usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
        "model_usage": result.get("modelUsage"),
        "answer": result.get("result") or "",
    }


Runner = Callable[[list[str], str, str, dict[str, str], int], tuple[int, list[str]]]


def _subprocess(argv_: list[str], stdin: str, cwd: str, env: dict[str, str], timeout: int) -> tuple[int, list[str]]:
    try:
        proc = subprocess.run(argv_, input=stdin, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return 124, out.splitlines()
    return proc.returncode, proc.stdout.splitlines()


def supports_effort(runner: Runner, cwd: str, env: dict[str, str]) -> bool:
    code, lines = runner([claude_code.BINARY, "--help"], "", cwd, env, 60)
    return code == 0 and any("--effort" in line for line in lines)


def environment(name: str, home: str) -> dict[str, str]:
    """agent-launch's environment for the agent, without the session note: root's claude, nothing of the home."""
    path = claude_code.PATH
    agent_bin = f"/etc/digipaddock/agent-bin/{name}"
    if os.path.isdir(agent_bin) and not os.path.islink(agent_bin) and os.stat(agent_bin).st_uid == 0:
        path = f"{agent_bin}:{path}"
    return {
        "PATH": path, "HOME": home, "LANG": "C.UTF-8", "KD_AGENT": name, "DISABLE_AUTOUPDATER": "1",
        "CLAUDE_CODE_ENABLE_AWAY_SUMMARY": "0", "BASH_DEFAULT_TIMEOUT_MS": "660000",
        "BASH_MAX_TIMEOUT_MS": "660000",
    }


def run(
    *,
    name: str,
    folder: str,
    settings: str,
    snapshots: Mapping[str, tuple[str, str]],
    out: Path,
    model: str,
    efforts: Sequence[str],
    repeats: int,
    home: str,
    hidden: Iterable[str] = (),
    repos: Mapping[str, tuple[str, str]] | None = None,
    timeout: int = 1800,
    runner: Runner = _subprocess,
    clock: Callable[[], float] = time.time,
) -> list[dict[str, Any]]:
    check_model(model)
    for effort in efforts:
        if effort not in EFFORTS:
            raise ProbeError(f"unknown effort {effort}")
    if out.exists() and any(out.iterdir()):
        raise ProbeError(f"{out} is not empty: every run of the probe writes into a fresh directory")
    memory = out / "hindsight-off.json"
    env = {**environment(name, home), "HINDSIGHT_CONFIG": str(memory)}
    cwd = f"{folder}/{launch.WORK}"
    if not supports_effort(runner, cwd, env):
        raise ProbeError("the installed claude has no --effort option: the probe cannot set the effort")
    out.mkdir(parents=True, exist_ok=True)
    memory.write_text(json.dumps(MEMORY_OFF, indent=2) + "\n", "utf-8")
    closed_cards = sorted({*snapshots, *(_card(card) for card in hidden)})
    pinned = bool(repos)
    own = str(out.resolve())
    merged = probe_settings(settings, closed_cards, folder, home, pinned, own)
    closed = closed_paths(folder, home, pinned, own)
    rows = []
    for repeat, card, effort in schedule(sorted(snapshots), efforts, repeats):
        stem = f"{card}-{effort}-{repeat}"
        args = argv(merged, folder, model, effort)
        started = clock()
        code, lines = runner(args, prompt(card, snapshots, closed_cards, repos), cwd, env, timeout)
        row = {
            "card": card, "effort": effort, "repeat": repeat, "requested_model": model, "exit": code,
            "wall_s": round(clock() - started, 1), "started": started,
            "leaks": leaks(lines, closed_cards, closed, pinned), **outcome(lines),
        }
        (out / f"{stem}.stream.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
        (out / f"{stem}.md").write_text(row["answer"] + "\n", "utf-8")
        with (out / "runs.jsonl").open("a", encoding="utf-8") as journal:
            journal.write(json.dumps({k: v for k, v in row.items() if k != "answer"}, ensure_ascii=False) + "\n")
        rows.append(row)
    return rows


def load_snapshots(directory: Path) -> dict[str, tuple[str, str]]:
    index = json.loads((directory / "snapshots.json").read_text("utf-8"))
    result = {}
    if not isinstance(index, dict) or not index:
        raise ProbeError("snapshots.json lists no snapshot")
    for card, meta in index.items():
        if not isinstance(meta, dict) or not {"source", "sha256"} <= meta.keys():
            raise ProbeError(f"{card}: snapshots.json has no source and sha256")
        data = (directory / f"{_card(card)}.md").read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise ProbeError(f"{card}: the snapshot changed after extraction")
        result[card] = (meta["source"], data.decode("utf-8"))
    return result


def load_repos(directory: Path) -> dict[str, tuple[str, str]]:
    """The pinned repositories of an extraction: name → (commit, absolute path); none — empty."""
    path = directory / "repos.json"
    index = json.loads(path.read_text("utf-8")) if path.exists() else {}
    result = {}
    for name, meta in index.items():
        target = directory / str(meta.get("path", "")) if isinstance(meta, dict) else None
        if target is None or not re.fullmatch(r"[0-9a-f]{40}", str(meta.get("commit"))) or not target.is_dir():
            raise ProbeError(f"repos.json: {name} has no commit or no unpacked copy")
        result[name] = (meta["commit"], str(target.resolve()))
    return result


# --- summary ---------------------------------------------------------------------------------------------------


def summary(rows: Iterable[Mapping[str, Any]]) -> str:
    """One line per card and effort: runs, errors, leaked runs, median time and turns, mean output tokens, total
    cost. Time, turns and tokens count only clean runs (no error, no leak)."""
    groups: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault((row["card"], row["effort"]), []).append(row)
    lines = ["card\teffort\truns\terrors\tleaked\tmodel\tmedian_s\tmedian_turns\tmean_output_tokens\tcost_usd"]
    for (card, effort), items in sorted(groups.items()):
        errors = [item for item in items if item.get("is_error") or item.get("exit") != 0]
        leaked = [item for item in items if item.get("leaks")]
        ok = [item for item in items if item not in errors and item not in leaked]
        models = ",".join(sorted({str(item.get("model")) for item in items}))
        seconds = [item["duration_ms"] / 1000 for item in ok if item.get("duration_ms") is not None]
        turns = [item["num_turns"] for item in ok if item.get("num_turns") is not None]
        tokens = [item["output_tokens"] for item in ok if item.get("output_tokens") is not None]
        cost = sum(item.get("total_cost_usd") or 0 for item in items)
        lines.append(
            f"{card}\t{effort}\t{len(items)}\t{len(errors)}\t{len(leaked)}\t{models}"
            f"\t{median(seconds) if seconds else '-'}\t{median(turns) if turns else '-'}"
            f"\t{round(sum(tokens) / len(tokens)) if tokens else '-'}\t{round(cost, 2)}"
        )
    return "\n".join(lines) + "\n"


# --- entry -----------------------------------------------------------------------------------------------------


def main(argv_: Sequence[str] | None = None, *, runner: Runner = _subprocess) -> int:
    parser = argparse.ArgumentParser(prog="analyst_effort_probe")
    sub = parser.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract", help="root: write the fixed snapshots")
    ext.add_argument("--out", required=True, type=Path)
    ext.add_argument("--db", default=CABINET_DB)
    ext.add_argument("--transcripts", type=Path, help="the agent's session transcripts (for --draft)")
    ext.add_argument("--brief", action="append", default=[], help="KD-<n>:<brief version>")
    ext.add_argument("--draft", action="append", default=[], help="KD-<n>:<revision of the description edit>")
    ext.add_argument("--repo", action="append", default=[], help="<name>=<repository copy>@<commit> to pin")
    one = sub.add_parser("run", help="as the agent's user: the probe sessions")
    one.add_argument("--agent", default="analyst")
    one.add_argument("--snapshots", required=True, type=Path)
    one.add_argument("--out", required=True, type=Path)
    one.add_argument("--model", required=True)
    one.add_argument("--efforts", default="medium,high")
    one.add_argument("--repeats", type=int, default=3)
    one.add_argument("--timeout", type=int, default=1800)
    one.add_argument(
        "--hide", action="append", required=True, help="a card that holds the known remarks (the control card): never opened"
    )
    table = sub.add_parser("summary", help="the table of runs.jsonl")
    table.add_argument("runs", type=Path)
    args = parser.parse_args(argv_)
    try:
        if args.command == "extract":
            items = [brief_version(args.db, *_pair(value)) for value in args.brief]
            if args.draft and args.transcripts is None:
                raise ProbeError("--draft needs --transcripts")
            items += [draft(args.transcripts, *_pair(value)) for value in args.draft]
            if len({item.card for item in items}) != len(items) or not items:
                raise ProbeError("give each card once, at least one")
            specs = [_repo_spec(value) for value in args.repo]
            if len({name for name, _, _ in specs}) != len(specs):
                raise ProbeError("give each repository once")
            repos = {name: pin_repo(args.out, name, source, rev) for name, source, rev in specs}
            print(json.dumps(write_snapshots(args.out, items, repos), indent=2, ensure_ascii=False))
        elif args.command == "run":
            import pwd

            name = args.agent
            user = pwd.getpwuid(os.geteuid()).pw_name
            if agents.user_of(name) != user:
                raise ProbeError(f"run as the agent's own user {agents.user_of(name)}, not {user}")
            folder = f"{launch.configured_agents_root()}/{name}"
            plan = launch.plan_generated(
                name, folder, uid=os.geteuid(), user=user, agents_root=launch.configured_agents_root(),
                fs=launch.HostFilesystem(), standing=False,  # a probe measures the model, not the web
            )
            rows = run(
                name=name, folder=folder, settings=plan.settings, snapshots=load_snapshots(args.snapshots),
                out=args.out, model=args.model, efforts=args.efforts.split(","), repeats=args.repeats,
                home=pwd.getpwuid(os.geteuid()).pw_dir, hidden=args.hide, repos=load_repos(args.snapshots),
                timeout=args.timeout, runner=runner,
            )
            sys.stdout.write(summary(rows))
        else:
            rows = [json.loads(line) for line in args.runs.read_text("utf-8").splitlines() if line.strip()]
            sys.stdout.write(summary(rows))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:  # ProbeError, LaunchError and broken JSON are ValueErrors
        print(f"analyst_effort_probe: {exc}", file=sys.stderr)
        return 1
