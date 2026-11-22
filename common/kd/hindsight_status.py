"""Read-only Hindsight diagnostics. A timer writes an allowlisted snapshot for the networkless cabinet.

No prompts, memory contents, API keys or upstream error bodies enter the snapshot. Only fixed HTTP GETs;
collecting diagnostics never invokes an LLM, seeds a bank, changes configuration or upgrades software.
"""
import json
import os
import re
import stat
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

SNAPSHOT = Path('/var/lib/digipaddock/hindsight-status/status.json')
# The server's env, written by hindsight_setup.sh install: the host runs the memory service (the collector's unit
# and the agents' plugin take the same mark). Root's file in a directory open to read: its presence is public.
ENV = Path('/etc/hindsight/coding-agent.env')
API = 'http://127.0.0.1:9077'
MAX_BYTES = 2_000_000
POLICY_FIELDS = ('autoUpdate', 'retainSessions', 'autoSeed', 'gitIngest', 'codebaseSurvey',
                 'pageTriggerCron', 'pageTriggerType', 'disabled')
STAT_FIELDS = ('total_documents', 'total_nodes', 'total_observations', 'pending_operations',
               'failed_operations', 'pending_consolidation', 'failed_consolidation')


def installed() -> bool:
    """The host runs the memory service."""
    return ENV.exists()


def read_json(path):
    """Bounded, nonblocking regular-file read; never follows a file symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return {}
            raw = handle.read(MAX_BYTES + 1)
        data = json.loads(raw) if len(raw) <= MAX_BYTES else None
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Unavailable(Exception):
    pass


def get_json(url, token=''):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urlopen(Request(url, headers=headers), timeout=5) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise Unavailable('response_too_large')
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise Unavailable('invalid_response')
        return data
    except HTTPError as exc:
        raise Unavailable('http_' + str(exc.code)) from None
    except (URLError, OSError, ValueError):
        raise Unavailable('unreachable') from None


def text(value):
    return re.sub(r'[\x00-\x1f\x7f]', '', value)[:160] if isinstance(value, str) else None


def public_url(value):
    if not isinstance(value, str):
        return None
    try:
        p = urlsplit(value)
        if p.scheme == 'https' and p.hostname and not (p.username or p.password or p.query or p.fragment):
            return value
    except ValueError:
        pass
    return None


def client_info(name, config, version):
    """Resolve the factory's agent directory, including Claude and bank overrides."""
    cfg = {**config, **config.get('harnesses', {}).get('claude-code', {})}
    folder = '/srv/digipaddock-agents/' + name + '/work'
    paths = cfg.get('mapPathToBank', {})
    matching = [p for p in paths if folder == p.rstrip('/') or folder.startswith(p.rstrip('/') + '/')]
    # Without an explicit mapping, git-aware dynamic resolution belongs to the plugin. Do not guess it.
    bank = paths[max(matching, key=len)] if matching else cfg.get('bankId')
    cfg = {**cfg, **cfg.get('banks', {}).get(bank, {})}
    bank = cfg.get('bank', bank)
    approved = not cfg.get('optInOnly', False) or bool(matching) or any(
        folder == p.rstrip('/') or folder.startswith(p.rstrip('/') + '/') for p in cfg.get('optInPaths', []))
    return {'agent': name, 'bank': text(bank) if config else None, 'version': text(version), 'configured': bool(config),
            'enabled': bool(config) and approved and not cfg.get('disabled', False),
            'policy': {k: cfg[k] for k in POLICY_FIELDS if k in cfg and isinstance(cfg[k], (str, bool, int))}}


def collect(get, clients, units, *, now, previous=None, web_url=None):
    out = {'collected_at': now, 'server': {'state': 'unknown', 'version': None}, 'units': units,
           'clients': clients, 'banks': [], 'banks_complete': False, 'web_url': public_url(web_url),
           'releases': (previous or {}).get('releases', {})}
    try:
        get('/health')
        out['server']['state'] = 'online'
    except Unavailable as exc:
        out['server'].update(state='unavailable', error=str(exc))
    try:
        version = get('/version')
        out['server']['version'] = text(version.get('api_version'))
        features = version.get('features')
        out['server']['audit_log'] = isinstance(features, dict) and features.get('audit_log') is True
    except Unavailable:
        pass
    try:
        banks = []
        for offset in range(0, 1000, 100):
            page = get('/v1/default/banks?limit=100&offset=' + str(offset))
            items = page.get('banks')
            if not isinstance(items, list):
                raise Unavailable('invalid_response')
            banks.extend(items)
            total = page.get('total')
            if len(items) < 100 or (type(total) is int and offset + len(items) >= total):
                out['banks_complete'] = True
                break
        for bank in banks:
            if not isinstance(bank, dict):
                out['banks_complete'] = False
                continue
            bid = text(bank.get('bank_id'))
            if not bid:
                continue
            row = {'id': bid, 'name': text(bank.get('name')), 'stats': None, 'activity': None}
            prefix = '/v1/default/banks/' + quote(bid, safe='')
            try:
                stats = get(prefix + '/stats')
                row['stats'] = {k: stats[k] for k in STAT_FIELDS
                                if type(stats.get(k)) is int and stats[k] >= 0}
                row['last_write'] = text(stats.get('last_memory_write_at'))
            except Unavailable as exc:
                row['stats_error'] = str(exc)
            if out['server'].get('audit_log'):
                try:
                    activity = get(prefix + '/audit-logs/stats?period=7d')
                    totals = {}
                    buckets = activity.get('buckets')
                    if not isinstance(buckets, list):
                        raise Unavailable('invalid_response')
                    for bucket in buckets:
                        if not isinstance(bucket, dict) or not isinstance(bucket.get('actions'), dict):
                            raise Unavailable('invalid_response')
                        for action, count in bucket.get('actions', {}).items():
                            if type(count) is int and count >= 0:
                                key = text(action)
                                totals[key] = totals.get(key, 0) + count
                    row['activity'] = totals
                except Unavailable as exc:
                    row['activity_error'] = str(exc)
            out['banks'].append(row)
    except Unavailable as exc:
        out['banks_error'] = str(exc)
    return out


def release_check(previous, now, fetch=get_json):
    """Public package metadata, once a day. A failed check is unknown, never 'up to date'."""
    try:
        age = datetime.fromisoformat(now) - datetime.fromisoformat(previous['checked_at'])
        if 0 <= age.total_seconds() < 86400:
            return previous
    except (KeyError, TypeError, ValueError):
        pass
    out = {'checked_at': now}
    for key, url in (('server', 'https://pypi.org/pypi/hindsight-api/json'),
                     ('client', 'https://registry.npmjs.org/@vectorize-io%2fhindsight-coding-agents/latest')):
        try:
            data = fetch(url)
            out[key] = text(data.get('info', {}).get('version') if key == 'server' else data.get('version'))
        except Unavailable:
            out[key] = None
    return out


def write_snapshot(path, data):
    fd, tmp = tempfile.mkstemp(prefix='.status-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle, ensure_ascii=False)
            handle.write('\n')
            os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def main():
    import pwd
    import subprocess
    from kd import agents
    now = datetime.now(timezone.utc).isoformat()
    clients = []
    for name in agents.names():
        home = Path(pwd.getpwnam(agents.user_of(name)).pw_dir)
        cfg = read_json(home / '.hindsight/coding-agent.json')
        package = read_json(home / '.hindsight/coding-agents/package.json')
        try:
            clients.append(client_info(name, cfg, package.get('version')))
        except (TypeError, AttributeError, ValueError):
            clients.append({'agent': name, 'bank': None, 'version': None, 'configured': False,
                            'enabled': False, 'policy': {}})
    units = {}
    for unit in ('hindsight-api', 'hindsight-worker', 'hindsight-backup.timer'):
        try:
            value = subprocess.run(['/usr/bin/systemctl', 'is-active', unit], capture_output=True,
                                   text=True, timeout=3).stdout.strip()
            units[unit] = value if value in ('active', 'inactive', 'failed', 'activating') else 'unknown'
        except (OSError, subprocess.TimeoutExpired):
            units[unit] = 'unknown'
    previous = read_json(SNAPSHOT)
    token = os.environ.get('HINDSIGHT_API_TENANT_API_KEY', '')
    deadline = time.monotonic() + 180
    def get(path):
        if time.monotonic() >= deadline:
            raise Unavailable('collection_timeout')
        return get_json(API + path, token)
    cabinet = read_json('/etc/digipaddock/cabinet.json')
    data = collect(get, clients, units, now=now, previous=previous, web_url=cabinet.get('hindsight_web_url'))
    data['releases'] = release_check(previous.get('releases', {}), now)
    write_snapshot(SNAPSHOT, data)
