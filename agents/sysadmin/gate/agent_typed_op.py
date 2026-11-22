#!/usr/bin/python3
"""agent-typed-op: the typed steps that change one thing on a target as root.

    agent-typed-op cron <KD-N> set <line>          one line of root's crontab, marked « # kd:<KD-N>»
    agent-typed-op cron <KD-N> remove
    agent-typed-op file <dir> <name> <KD-N> set <text>    one file of /etc/sysctl.d or /etc/logrotate.d
    agent-typed-op file <dir> <name> <KD-N> remove
    agent-typed-op script off|on                    the target's switch that refuses script steps
    agent-typed-op reboot <KD-N>                    system.reboot: the snapshot, then systemctl reboot

The exec gate runs it as a step of a plan the owner signed (``python3 -I /usr/local/libexec/agent-typed-op …``,
without a shell) after it has read the content with its policy (kd.exec_policy: ``cron_problem``, ``drop_problem``).
Here only the shape is checked again; what it does is narrow on purpose:

- cron: root's crontab is read with ``crontab -l`` and written whole with ``crontab -`` (never its spool file). The
  card's mark makes the line one per card: ``set`` replaces the card's line or adds it, ``remove`` takes it away;
  every other line stays as it was. Before the first change of a card its crontab is copied to
  ``/root/kd-<n>/crontab.before`` (a later step of the card does not overwrite that copy).
- file: one regular file (never a link) of the two directories; its earlier state goes to ``/root/kd-<n>/<name>.before``
  the same way, the new text is written beside it under a hidden name and renamed over it (root, 0644). A sysctl file
  is loaded at once (``sysctl -p <file>``); taking one away does not return the values it set — the step says so.
- script: the file ``/etc/agent-exec-gate/script-off`` — while it exists the gate refuses every script step on this
  server, in both modes; ``on`` removes it. Turning it either way is an ordinary signed plan step.
- reboot: only as the critical action ``system.reboot`` the owner signs as an exact command (the policy calls this
  helper's reboot critical in a plan step); the routing rules, WireGuard, listeners, running services and containers
  go to ``/root/kd-<n>/before-reboot-<UTC>/``, then ``systemctl reboot``.

Exit 0 and one line of what changed; exit 2 and the reason otherwise.
Python 3.6+ and stdlib only: the targets include AlmaLinux 8 with the system Python 3.6."""

import os
import re
import stat
import subprocess
import sys
import time

TASK = re.compile(
    r"[A-Z]{1,8}-[0-9]{1,8}\Z"
)  # a card number (kd.ids.CARD); its copies go to /root/<it, lower>/
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\Z")
DIRS = ("/etc/sysctl.d", "/etc/logrotate.d")
SCRIPT_OFF = "/etc/agent-exec-gate/script-off"
COPIES = "/root"
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin"
MARK = " # kd:"
TIMEOUT = 60


class Refuse(Exception):
    pass


def _tool(argv, stdin=None):
    """(exit code, stdout, stderr) of a system tool on a fixed PATH."""
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={"PATH": SAFE_PATH, "LC_ALL": "C"},
            timeout=TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise Refuse("%s не запустился: %s" % (argv[0], error.__class__.__name__))
    return proc.returncode, proc.stdout, proc.stderr


def _task(word):
    if not TASK.match(word):
        raise Refuse("метка — номер карточки вида KD-123")
    return word.lower()


def _copy_once(number, name, data):
    """The state before the card's first change: /root/kd-<n>/<name>; a later step of the card keeps it."""
    folder = os.path.join(COPIES, number)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, name)
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except FileExistsError:
        return path
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return path


def read_crontab():
    code, out, err = _tool(["crontab", "-l"])
    if code == 0:
        return out
    if b"no crontab" in err.lower():
        return b""
    raise Refuse("crontab -l: код %d" % code)


def write_crontab(data):
    code, _, err = _tool(["crontab", "-"], stdin=data)
    if code != 0:
        raise Refuse(
            "crontab -: код %d: %s"
            % (code, err.decode("utf-8", "replace").strip()[:200])
        )


def cron(argv, read=read_crontab, write=write_crontab):
    if (
        len(argv) not in (2, 3)
        or argv[1] not in ("set", "remove")
        or (argv[1] == "set") != (len(argv) == 3)
    ):
        raise Refuse("нужны метка карточки и set <строка> или remove")
    number, action = _task(argv[0]), argv[1]
    mark = MARK + argv[0]
    if action == "set" and (
        "\n" in argv[2] or "\r" in argv[2] or not argv[2].strip() or MARK in argv[2]
    ):
        raise Refuse("строка cron — одна непустая строка без метки kd:")
    data = read()
    lines = data.decode("utf-8", "surrogateescape").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    mine = [i for i, line in enumerate(lines) if line.endswith(mark)]
    if len(mine) > 1:
        raise Refuse(
            "в crontab root несколько строк с меткой %s: не берусь выбирать" % argv[0]
        )
    new = argv[2].strip() + mark if action == "set" else None
    if action == "remove" and not mine:
        return "cron %s: строки нет, без изменений" % argv[0]
    if mine and lines[mine[0]] == new:
        return "cron %s: строка уже такая, без изменений" % argv[0]
    _copy_once(number, "crontab.before", data)
    if mine and new is None:
        del lines[mine[0]]
        verdict = "убрана"
    elif mine:
        lines[mine[0]] = new
        verdict = "заменена"
    else:
        lines.append(new)
        verdict = "добавлена"
    write(
        ("\n".join(lines) + "\n").encode("utf-8", "surrogateescape") if lines else b""
    )
    return "cron %s: строка %s (копия до правки: %s/%s/crontab.before)" % (
        argv[0],
        verdict,
        COPIES,
        number,
    )


def _regular_or_none(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise Refuse("%s — не обычный файл" % path)
    return info


def drop(argv, load=None):
    if (
        len(argv) not in (4, 5)
        or argv[3] not in ("set", "remove")
        or (argv[3] == "set") != (len(argv) == 5)
    ):
        raise Refuse("нужны каталог, имя, метка карточки и set <текст> или remove")
    directory, name, task, action = argv[:4]
    if directory not in DIRS:
        raise Refuse("каталог не из списка (%s)" % ", ".join(DIRS))
    if not NAME.match(name) or ".." in name:
        raise Refuse("имя файла — одно простое имя")
    number = _task(task)
    folder_info = os.lstat(directory)
    if not stat.S_ISDIR(folder_info.st_mode):
        raise Refuse("%s — не каталог" % directory)
    path = os.path.join(directory, name)
    info = _regular_or_none(path)
    old = b""
    if info is not None:
        with open(path, "rb") as handle:
            old = handle.read()
    if action == "remove":
        if info is None:
            return "%s: файла нет, без изменений" % path
        _copy_once(number, name + ".before", old)
        os.unlink(path)
        tail = (
            "; значения sysctl, которые он задал, остаются до перезагрузки"
            if directory == DIRS[0]
            else ""
        )
        return "%s: убран (копия: %s/%s/%s.before)%s" % (
            path,
            COPIES,
            number,
            name,
            tail,
        )
    text = argv[4] if argv[4].endswith("\n") else argv[4] + "\n"
    new = text.encode("utf-8")
    if info is not None and old == new:
        verdict = "уже такой, без изменений"
    else:
        if info is not None:
            _copy_once(number, name + ".before", old)
        draft = os.path.join(directory, ".%s.kd-new" % name)
        try:
            fd = os.open(
                draft,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
        except FileExistsError:
            raise Refuse("%s уже лежит: чья-то незаконченная правка" % draft)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(new)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(draft, 0o644)
            os.replace(draft, path)
        except OSError:
            try:
                os.unlink(draft)
            except OSError:
                pass
            raise
        verdict = (
            "записан"
            if info is None
            else "заменён (копия: %s/%s/%s.before)" % (COPIES, number, name)
        )
    if directory == DIRS[0]:
        code, out, err = (load or _tool)(["sysctl", "-p", path])
        if code != 0:
            raise Refuse(
                "%s: %s, но sysctl -p: код %d: %s"
                % (path, verdict, code, err.decode("utf-8", "replace").strip()[:200])
            )
        return "%s: %s; sysctl -p применил его" % (path, verdict)
    return "%s: %s" % (path, verdict)


def script(argv, flag=None):
    flag = flag or SCRIPT_OFF
    if argv not in (["on"], ["off"]):
        raise Refuse("нужно on или off")
    exists = os.path.lexists(flag)
    if argv == ["off"]:
        if exists:
            return "script: уже выключен, без изменений"
        fd = os.open(
            flag,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o644,
        )
        os.close(fd)
        return "script: выключен — шлюз этого сервера отказывает в шагах-скриптах"
    if not exists:
        return "script: уже включён, без изменений"
    os.unlink(flag)
    return "script: включён"


# system.reboot (a critical action): what the server looked like before it, then the reboot itself
SNAPSHOT = (
    ("rule4.txt", ["ip", "-4", "rule", "show"]),
    ("rule6.txt", ["ip", "-6", "rule", "show"]),
    ("wg.txt", ["wg", "show"]),
    ("ss.txt", ["ss", "-tlnpu"]),
    (
        "running.txt",
        [
            "systemctl",
            "list-units",
            "--type=service",
            "--state=running",
            "--no-legend",
            "--plain",
        ],
    ),
    ("docker.txt", ["docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Status}}"]),
)


def reboot(argv, run=None, stamp=None):
    """The snapshot into /root/<card>/before-reboot-<UTC time>/ (a part whose tool is missing writes its error
    there and does not stop the reboot), then ``systemctl reboot``."""
    if len(argv) != 1:
        raise Refuse("нужна метка карточки")
    card = _task(argv[0])
    run = run or _tool
    folder = os.path.join(
        COPIES,
        card,
        "before-reboot-%s" % (stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())),
    )
    os.makedirs(folder, mode=0o700)
    for name, argv_part in SNAPSHOT:
        try:
            code, out, err = run(argv_part)
        except Refuse as refusal:
            code, out, err = 127, b"", str(refusal).encode("utf-8")
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(
                out
                + (
                    b""
                    if code == 0
                    else b"\n# exit %d: %s\n" % (code, err.strip()[:500])
                )
            )
    code, _, err = run(["systemctl", "reboot"])
    if code != 0:
        raise Refuse(
            "снимок в %s, но systemctl reboot: код %d: %s"
            % (folder, code, err.decode("utf-8", "replace").strip()[:200])
        )
    return "снимок в %s; перезагрузка запущена" % folder


def main(argv, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    verbs = {"cron": cron, "file": drop, "script": script, "reboot": reboot}
    try:
        if not argv or argv[0] not in verbs:
            raise Refuse("нужно cron, file, script или reboot")
        line = verbs[argv[0]](list(argv[1:]))
    except Refuse as refusal:
        err.write("agent-typed-op: %s\n" % refusal)
        return 2
    except OSError as error:
        err.write("agent-typed-op: %s\n" % (error.strerror or error.__class__.__name__))
        return 2
    out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
