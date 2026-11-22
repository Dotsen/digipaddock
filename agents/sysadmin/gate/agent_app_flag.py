#!/usr/bin/python3
"""agent-app-flag: set one switch of an application's ini file or env file.

    agent-app-flag <file> <section> <key> <0|1>
    agent-app-flag --env <file> <key> <value>

The exec gate runs it as a step ``app.flag`` of a plan the owner signed: as the application's own account, in the
application's directory (``runuser -u <user> -- env -C <dir> python3 -I <this file> …``). It is the whole of what
such a step can do, so it is narrow on purpose:

- ``<file>`` is one name in the current directory — a regular file, never a symlink, never a path;
- ``<key>`` is a switch (``…_enabled``) and the value is ``0`` or ``1``: it cannot write an address, a token or a
  password, and it prints only a switch's old value;
- the section must exist once. Inside it the one line of the key is replaced, or — when there is none — the line is
  added after the section's last non-empty line. Two lines of the key are a refusal. Every other byte of the file
  stays as it was, line endings included. The file is read as configparser reads it — a section's name exactly, a
  key's name in any case — and an indented line of the key, which configparser reads by the indent around it, is a
  refusal: a second line of the key would stop the application at its next start;
- the day's first state is kept in ``<file>.bak-YYYY-MM-DD`` (a later step of the same day does not overwrite it);
  the new content is written to ``<file>.new`` and renamed over the file, so a reader never sees half of it;
- a value that is already set changes nothing and is not an error.

The env form sets one key of an env file (``.env``): which keys and which values is the catalog's list, checked
before the plan is signed; here the value is one plain word (letters, digits, ``_.-``), so it is no address, token
or password. A line of the key is ``<key> = <value>``, with or without spaces, quotes or a leading ``export``; a
commented line is not the key. One such line is replaced, none — the line is added at the end of the file, two — a
refusal. The old value is printed only for a plain word. Other values use the localized placeholder. The backup, the
atomic write and the unchanged bytes are the ini form's.

Exit 0 prints the prior and new flag values, using a placeholder for an absent value. Exit 2 reports a refusal.
the reason otherwise.
Python 3.6+ and stdlib only: the targets include AlmaLinux 8 with the system Python 3.6."""

import os
import re
import stat
import sys
import time

FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
SECTION = re.compile(r"[a-z][a-z0-9_]{0,60}")
KEY = re.compile(r"[a-z][a-z0-9_]{0,60}_enabled")
ENV_FILE = re.compile(r"\.?[A-Za-z0-9][A-Za-z0-9._-]{0,80}")
ENV_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,80}")
ENV_VALUE = re.compile(r"[A-Za-z0-9_.-]{1,40}")
ENV_OTHER = "другое"
HEADER = re.compile(rb"\[([^\]\r\n]+)\][ \t]*(?:[#;].*)?$")
MAX_BYTES = 1024 * 1024


class Refuse(Exception):
    pass


def split_eol(line):
    for eol in (b"\r\n", b"\n", b"\r"):
        if line.endswith(eol):
            return line[: -len(eol)], eol
    return line, b""


def rewrite(data, section, key, value):
    """(new bytes, the old value or None). Raises Refuse."""
    lines = data.splitlines(True)
    wanted = section.encode("ascii")
    # as configparser reads a key: its name in any case, "=" or ":" after it
    line_of_key = re.compile(
        rb"([ \t]*)" + re.escape(key.encode("ascii")) + rb"[ \t]*[=:](.*)$", re.I
    )
    start = None
    end = len(lines)
    for i, line in enumerate(lines):
        header = HEADER.match(split_eol(line)[0])
        if not header:
            continue
        if start is not None and end == len(lines):
            end = i
        if header.group(1) == wanted:  # exact, as configparser names a section
            if start is not None:
                raise Refuse("секция [%s] встречается в файле дважды" % section)
            start = i
    if start is None:
        raise Refuse("в файле нет секции [%s]" % section)
    found = []
    last = start  # the section's last non-empty line: a new key goes right after it
    for i in range(start + 1, end):
        body = split_eol(lines[i])[0]
        if body.strip():
            last = i
        match = line_of_key.match(body)
        if match and match.group(1):
            # configparser takes an indented line for a key or for the value above it, by the indent around it;
            # a guess here could leave the key twice, and the application would not read its config at all
            raise Refuse(
                "строка ключа %s в секции [%s] написана с отступом: не берусь судить, ключ это или "
                "продолжение значения" % (key, section)
            )
        if match:
            found.append((i, match.group(2).strip()))
    if len(found) > 1:
        raise Refuse("ключ %s задан в секции [%s] дважды" % (key, section))
    text = ("%s = %s" % (key, value)).encode("ascii")
    if found:
        i, old = found[0]
        if old == value.encode("ascii"):
            return data, value
        lines[i] = text + split_eol(lines[i])[1]
        return b"".join(lines), old.decode("utf-8", "replace")[:20]
    body, eol = split_eol(lines[last])
    if not eol:  # the file ends without a newline: the line above gets one, of the file's own kind
        eol = split_eol(lines[0])[1] or b"\n"
        lines[last] = body + eol
    lines.insert(last + 1, text + eol)
    return b"".join(lines), None


def rewrite_env(data, key, value):
    """(new bytes, the old value as shown or None) for an env file. Raises Refuse."""
    lines = data.splitlines(True)
    line_of_key = re.compile(
        rb"[ \t]*(?:export[ \t]+)?" + re.escape(key.encode("ascii")) + rb"[ \t]*=(.*)$"
    )
    found = []
    for i, line in enumerate(lines):
        match = line_of_key.match(split_eol(line)[0])
        if match:
            found.append((i, match.group(1).strip()))
    if len(found) > 1:
        raise Refuse("ключ %s задан в файле дважды" % key)
    text = ("%s = %s" % (key, value)).encode("ascii")
    if found:
        i, old = found[0]
        if len(old) >= 2 and old[:1] == old[-1:] and old[:1] in (b'"', b"'"):
            old = old[1:-1]
        if old == value.encode("ascii"):
            return data, value
        shown = ENV_OTHER  # a value that is not a plain word may be a secret: never printed
        if re.fullmatch(ENV_VALUE.pattern.encode("ascii"), old):
            shown = old.decode("ascii")
        lines[i] = text + split_eol(lines[i])[1]
        return b"".join(lines), shown
    eol = (split_eol(lines[0])[1] if lines else b"") or b"\n"
    if lines and not split_eol(lines[-1])[1]:
        lines[-1] = lines[-1] + eol
    lines.append(text + eol)
    return b"".join(lines), None


def write_new(path, data, mode):
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_BINARY", 0)
    )
    fd = os.open(path, flags, mode)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, mode)  # the umask does not narrow the mode of the original
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view) :]
        os.fsync(fd)
    finally:
        os.close(fd)


def change(argv, cwd, today):
    if argv[:1] == ["--env"]:
        if len(argv) != 4:
            raise Refuse("нужны --env, файл, ключ и значение")
        name, key, value = argv[1:]
        if not ENV_FILE.fullmatch(name) or ".." in name:
            raise Refuse("файл — одно имя в каталоге приложения, не путь")
        if not ENV_KEY.fullmatch(key):
            raise Refuse("ключ — латинские буквы, цифры, «_» и «.», с буквы")
        if not ENV_VALUE.fullmatch(value):
            raise Refuse("значение — одно простое слово: латинские буквы, цифры, «_», «.» и «-»")
        label = "%s %s" % (name, key)

        def edit(data):
            return rewrite_env(data, key, value)
    else:
        if len(argv) != 4:
            raise Refuse("нужны файл, секция, ключ и значение 0 или 1")
        name, section, key, value = argv
        if not FILE.fullmatch(name) or ".." in name:
            raise Refuse("файл — одно имя в каталоге приложения, не путь")
        if not SECTION.fullmatch(section):
            raise Refuse("имя секции — строчные латинские буквы, цифры и «_»")
        if not KEY.fullmatch(key):
            raise Refuse("ключ не выключатель: его имя должно кончаться на _enabled")
        if value not in ("0", "1"):
            raise Refuse("значение выключателя — 0 или 1")
        label = "[%s] %s" % (section, key)

        def edit(data):
            return rewrite(data, section, key, value)
    path = os.path.join(cwd, name)
    try:
        info = os.lstat(path)
    except OSError:
        raise Refuse("файла %s нет в каталоге приложения" % name)
    if not stat.S_ISREG(info.st_mode):
        raise Refuse("%s — не обычный файл" % name)
    if info.st_size > MAX_BYTES:
        raise Refuse("%s больше 1 МБ" % name)
    fd = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    )
    try:
        with os.fdopen(fd, "rb") as handle:
            data = handle.read(MAX_BYTES + 1)
    except OSError as error:
        raise Refuse("%s не читается: %s" % (name, error.strerror))
    new, old = edit(data)
    shown = "нет" if old is None else old
    if new == data:
        return "%s: %s -> %s, без изменений" % (label, shown, value)
    mode = stat.S_IMODE(info.st_mode)
    draft = path + ".new"
    try:
        write_new(
            draft, new, mode
        )  # first: a draft someone left stops the step before anything is written
    except FileExistsError:
        raise Refuse("%s.new уже лежит рядом: чья-то незаконченная правка" % name)
    except OSError as error:
        raise Refuse("%s не записан: %s" % (name, error.strerror))
    try:
        try:
            write_new("%s.bak-%s" % (path, today), data, mode)
        except FileExistsError:
            pass  # the day's first state is the one to return to
        os.replace(draft, path)
    except OSError as error:
        try:
            os.unlink(draft)
        except OSError:
            pass
        raise Refuse("%s не записан: %s" % (name, error.strerror))
    return "%s: %s -> %s" % (label, shown, value)


def main(argv, cwd=None, today=None, out=None, err=None):
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        line = change(
            list(argv), cwd or os.getcwd(), today or time.strftime("%Y-%m-%d")
        )
    except Refuse as refusal:
        err.write("agent-app-flag: %s\n" % refusal)
        return 2
    out.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
