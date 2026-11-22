#!/usr/bin/python3
"""agent-hindsight-usage: token usage of the Hindsight server's own LLM calls, for checking kd-usage.

The agent-ro-gate runs it as root (sudo -n), without arguments. Read only. The Hindsight claude-code provider gives
its CLI a CLAUDE_CONFIG_DIR of its own (mkdtemp hindsight-claude-code-) inside the unit's PrivateTmp, so the CLI
transcripts live in /tmp/systemd-private-<boot>-<unit>-<x>/tmp/hindsight-claude-code-<x>/projects (root only, gone
on restart); kd-usage daily reads them at 00:30 and writes category hindsight rows into usage.jsonl. This prints,
as `section<TAB>object<TAB>value` lines (ledger first), only numbers and model names — never transcript text:

  unit    <unit>                    started=<UTC> jsonl=<N> bytes=<N> newest=<UTC>   (none — no PrivateTmp found)
  source  <day> <unit> <model>  requests= sessions= input= output= cache_write= cache_read=
  total   <day> <model>         the same, one API call (message.id + requestId) once across units, as kd-usage
  ledger  <day> <model>         coverage= and the numbers of the usage.jsonl hindsight rows, last LEDGER_DAYS days

A day is of the factory host's zone (host.json timezone, as kd-usage counts it): target_setup.sh
--timezone writes it into ZONE of the installed copy, the system's tzdata gives its offsets.

Files are read without following symlinks; only regular files. Python 3.6+ and stdlib only: the gate's targets
include AlmaLinux 8 with the system Python 3.6."""
import json
import os
import re
import stat
import sys
import time
from datetime import datetime, timedelta, timezone

VERSION = "# agent-hindsight-usage v1"
TMP_ROOT = "/tmp"
LEDGER = "/var/lib/digipaddock/usage.jsonl"
UNITS = ("hindsight-api.service", "hindsight-worker.service")
CONFIG_PREFIX = "hindsight-claude-code-"
ZONE = "UTC"  # the factory host's zone: target_setup.sh --timezone writes it here
LEDGER_DAYS = 14
FIELDS = ("requests", "sessions", "input", "output", "cache_write", "cache_read")
USAGE_KEYS = (("input", "input_tokens"), ("output", "output_tokens"),
              ("cache_write", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens"))
VALUE_MAX = 80
TS_RE = re.compile(r"^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?(Z|[+-]\d\d:?\d\d)?$")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _clean(text):
    """A value from a file: one line, printable, short."""
    return re.sub(r"[\x00-\x1f\x7f\s]+", " ", str(text)).strip()[:VALUE_MAX] or "?"


def _utc(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _zone_day(moment):
    """The day of an aware moment in ZONE; the process's own TZ is left as it was."""
    if ZONE == "UTC":
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%d")
    before = os.environ.get("TZ")
    os.environ["TZ"] = ZONE
    try:
        time.tzset()
        return time.strftime("%Y-%m-%d", time.localtime(moment.timestamp()))
    finally:
        if before is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = before
        time.tzset()


def host_day(value):
    """The host's day of an ISO timestamp; not a timestamp — None. No zone is taken as UTC (kd-usage would take the host's
    local time) — the CLI always writes `…Z`, so the two agree on real transcripts."""
    m = TS_RE.match(value) if isinstance(value, str) else None
    if not m:
        return None
    try:
        moment = datetime(*(int(m.group(i)) for i in range(1, 7)), tzinfo=timezone.utc)
    except ValueError:
        return None
    zone = m.group(7)
    if zone and zone != "Z":
        sign = -1 if zone[0] == "-" else 1
        digits = zone[1:].replace(":", "")
        moment -= sign * timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
    return _zone_day(moment)


def _dir_of(path, owner, mode=None):
    """lstat of a real directory of this owner (and exactly this mode, if given); otherwise None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != owner or (mode is not None and stat.S_IMODE(st.st_mode) != mode):
        return None
    return st


def private_tmps(tmp_root, units=UNITS, owner=0):
    """{unit: [(its /tmp, start time)]}. systemd makes systemd-private-<boot>-<unit>-<x> (root, 0700) with one tmp/
    inside at unit start; /tmp is open to all, so another owner or mode is a fake and is skipped, as in kd-usage."""
    found = dict((unit, []) for unit in units)
    try:
        names = sorted(os.listdir(tmp_root))
    except OSError:
        return found
    for name in names:
        unit = next((u for u in units if name.startswith("systemd-private-") and "-%s-" % u in name), None)
        if unit is None:
            continue
        outer = os.path.join(tmp_root, name)
        st = _dir_of(outer, owner, 0o700)
        if st is None or _dir_of(os.path.join(outer, "tmp"), owner) is None:
            continue
        found[unit].append((os.path.join(outer, "tmp"), st.st_mtime))
    return found


def _real_dir(path):
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def transcripts(private_tmp):
    """Paths of .jsonl under hindsight-claude-code-*/projects; symlinks are skipped."""
    try:
        names = sorted(os.listdir(private_tmp))
    except OSError:
        return
    for name in names:
        projects = os.path.join(private_tmp, name, "projects")
        if not name.startswith(CONFIG_PREFIX) or not _real_dir(os.path.join(private_tmp, name)) \
                or not _real_dir(projects):
            continue
        for dirpath, _, files in os.walk(projects, followlinks=False):
            for file in sorted(files):
                path = os.path.join(dirpath, file)
                if file.endswith(".jsonl") and not os.path.islink(path):
                    yield path


def _open_regular(path):
    """(file object, size) of a regular file without following a symlink; anything else — None."""
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None
        return os.fdopen(fd, "rb"), st
    except OSError:
        os.close(fd)
        return None


def usage_line(raw):
    """(day, call key, model, numbers, sessionId) of a model answer line; other lines — None."""
    try:
        entry = json.loads(raw.decode("utf-8", "replace"))
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            return None
        message = entry["message"]
        usage = message["usage"]
        day = host_day(entry.get("timestamp"))
        if day is None or not isinstance(usage, dict):
            return None
        numbers = dict((name, int(usage.get(src) or 0)) for name, src in USAGE_KEYS)
        key = (message.get("id"), entry.get("requestId"))
        return day, key, _clean(message.get("model") or "unknown"), numbers, entry.get("sessionId")
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def _add(records, key, model, numbers, session):
    record = records.setdefault(key, [model, dict((name, 0) for name, _ in USAGE_KEYS), set()])
    record[1] = dict((name, max(record[1][name], value)) for name, value in numbers.items())
    if session:
        record[2].add(str(session))


def _rows(records):
    """{(day, model): numbers} from {(day, key): [model, numbers, sessions]}."""
    rows, sessions = {}, {}
    for (day, _), (model, numbers, ids) in records.items():
        row = rows.setdefault((day, model), dict((f, 0) for f in FIELDS))
        row["requests"] += 1
        for name, value in numbers.items():
            row[name] += value
        sessions.setdefault((day, model), set()).update(ids)
    for k, ids in sessions.items():
        rows[k]["sessions"] = len(ids)
    return rows


def _numbers(row):
    return " ".join("%s=%s" % (f, row.get(f, "-")) for f in FIELDS)


def scan(tmp_root, units=UNITS, owner=0):
    """Lines of the unit, source and total sections."""
    unit_lines, source_lines = [], []
    total, anonymous = {}, [0]

    def key_of(key):
        if key == (None, None):  # no id at all: every such line is its own call
            anonymous[0] += 1
            return ("anonymous", anonymous[0])
        return key

    for unit, places in private_tmps(tmp_root, units, owner).items():
        if not places:
            unit_lines.append(("unit", unit, "none"))
            continue
        records = {}
        for private, started in sorted(places, key=lambda p: p[1]):
            count, size, newest = 0, 0, None
            for path in transcripts(private):
                opened = _open_regular(path)
                if opened is None:
                    continue
                handle, st = opened
                count, size = count + 1, size + st.st_size
                newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
                try:
                    with handle:
                        for raw in handle:
                            found = usage_line(raw)
                            if found is None:
                                continue
                            day, key, model, numbers, session = found
                            key = key_of(key)
                            _add(records, (day, key), model, numbers, session)
                            _add(total, (day, key), model, numbers, session)
                except OSError:  # one unreadable file must not hide the others, as in kd-usage
                    continue
            unit_lines.append(("unit", unit, "started=%s jsonl=%d bytes=%d newest=%s" % (
                _utc(started), count, size, _utc(newest) if newest is not None else "-")))
        for (day, model), row in sorted(_rows(records).items()):
            source_lines.append(("source", "%s %s %s" % (day, unit, model), _numbers(row)))
    total_lines = [("total", "%s %s" % (day, model), _numbers(row)) for (day, model), row in sorted(_rows(total).items())]
    return unit_lines + sorted(source_lines) + total_lines


def ledger(path, today, days=LEDGER_DAYS):
    """Lines of the ledger section: usage.jsonl rows of category hindsight from the last `days` days."""
    first = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    opened = _open_regular(path)
    if opened is None:
        return [("ledger", "-", "no file")]
    handle, _ = opened
    out = []
    with handle:
        for raw in handle:
            try:
                row = json.loads(raw.decode("utf-8", "replace"))
            except ValueError:
                continue
            if not isinstance(row, dict) or row.get("category") != "hindsight":
                continue
            day = row.get("day")
            if not isinstance(day, str) or not re.match(r"^\d{4}-\d\d-\d\d$", day) or day < first:
                continue
            numbers = dict((f, row[f]) for f in FIELDS if isinstance(row.get(f), int))
            out.append(("ledger", "%s %s" % (day, _clean(row.get("model", "-"))),
                        "coverage=%s %s" % (_clean(row.get("coverage", "-")), _numbers(numbers))))
    return sorted(out) or [("ledger", "-", "no hindsight rows since %s" % first)]


def blocks(tmp_root=TMP_ROOT, ledger_path=LEDGER, now=None, owner=0):
    """Text blocks in output order. The cheap ledger goes first: if scanning the transcripts outlives the gate's
    timeout, the part already flushed still reaches the sysadmin."""
    now = now or datetime.now(timezone.utc)
    today = _zone_day(now)
    yield "%s\nnow\t%s\tdays are %s\n" % (VERSION, now.strftime("%Y-%m-%dT%H:%M:%SZ"), ZONE)
    for section, fn in (("ledger", lambda: ledger(ledger_path, today)), ("unit", lambda: scan(tmp_root, owner=owner))):
        try:
            items = fn()
        except Exception as exc:  # noqa: BLE001 — one section must not hide the other
            items = [(section, "error", "%s: %s" % (type(exc).__name__, _clean(exc)))]
        yield "".join("\t".join(item) + "\n" for item in items)


def render(tmp_root=TMP_ROOT, ledger_path=LEDGER, now=None, owner=0):
    return "".join(blocks(tmp_root, ledger_path, now, owner))


def main(argv):
    if argv:
        sys.stderr.write("usage: agent-hindsight-usage (без аргументов)\n")
        return 2
    for block in blocks():
        sys.stdout.buffer.write(block.encode("utf-8"))
        sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
