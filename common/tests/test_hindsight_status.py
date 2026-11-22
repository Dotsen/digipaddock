import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kd import clock
from kd import hindsight_status as hs

NOW = '2026-10-24T10:00:00+00:00'
SECRET = 'never-publish-api-keys-or-memory-text'


def test_client_policy_uses_longest_path_harness_and_bank_overrides():
    config = {'apiToken': SECRET, 'retainSessions': False, 'autoUpdate': False,
              'optInOnly': True, 'mapPathToBank': {'/srv': 'shared', '/srv/digipaddock-agents/dev': 'dev'},
              'harnesses': {'claude-code': {'autoUpdate': True}},
              'banks': {'dev': {'bank': 'project::a', 'gitIngest': 'message'}}}
    found = hs.client_info('dev', config, '0.8.0')
    assert found['bank'] == 'project::a' and found['enabled']
    assert found['policy'] == {'retainSessions': False, 'autoUpdate': True, 'gitIngest': 'message'}
    assert SECRET not in json.dumps(found)
    assert hs.client_info('boss', {'optInOnly': True}, '0.8.0')['enabled'] is False
    assert hs.client_info('boss', {}, None)['bank'] is None


def fake_api(path):
    if path == '/health':
        return {'status': 'healthy', 'secret': SECRET}
    if path == '/version':
        return {'api_version': '0.10.2', 'features': {'audit_log': True}, 'secret': SECRET}
    if path.startswith('/v1/default/banks?'):
        return {'banks': [{'bank_id': 'coding-agent::dev', 'name': 'Dev', 'background': SECRET}], 'total': 1}
    if path.endswith('/stats'):
        return {'total_documents': 5, 'failed_operations': 2, 'last_memory_write_at': NOW, 'memory': SECRET}
    if path.endswith('/audit-logs/stats?period=7d'):
        return {'buckets': [{'actions': {'recall': 2, 'retain': 1}}, {'actions': {'recall': 4}}], 'secret': SECRET}
    raise AssertionError(path)


def test_collect_only_publishes_aggregates():
    result = hs.collect(fake_api, [], {'hindsight-api': 'active'}, now=NOW)
    assert result['server']['state'] == 'online'
    assert result['banks_complete'] is True
    bank = result['banks'][0]
    assert bank['stats'] == {'total_documents': 5, 'failed_operations': 2}
    assert bank['activity'] == {'recall': 6, 'retain': 1}
    assert SECRET not in json.dumps(result)


def test_failed_requests_do_not_turn_into_empty_or_healthy_data():
    def offline(path):
        raise hs.Unavailable('http_401')
    found = hs.collect(offline, [], {}, now=NOW)
    assert found['server']['state'] == 'unavailable'
    assert found['banks_complete'] is False
    assert found['banks_error'] == 'http_401'


def test_bank_statistics_can_fail_independently_and_audit_disabled_is_unknown():
    def partial(path):
        if path == '/version':
            return {'api_version': '0.9.2', 'features': {'audit_log': False}}
        if path.endswith('/stats'):
            raise hs.Unavailable('http_404')
        assert 'audit-logs' not in path
        return fake_api(path)
    found = hs.collect(partial, [], {}, now=NOW)
    assert found['banks'][0]['stats'] is None
    assert found['banks'][0]['activity'] is None
    assert found['banks'][0]['stats_error'] == 'http_404'


def test_pagination_keeps_all_banks():
    def paged(path):
        if path.endswith('offset=0'):
            return {'banks': [{'bank_id': 'bank' + str(i)} for i in range(100)], 'total': 101}
        if path.endswith('offset=100'):
            return {'banks': [{'bank_id': 'last'}], 'total': 101}
        if path == '/health':
            return {}
        raise hs.Unavailable('http_404')
    found = hs.collect(paged, [], {}, now=NOW)
    assert found['banks_complete'] and len(found['banks']) == 101


def test_release_checks_are_cached_for_a_day_and_failure_is_unknown():
    seen = []
    def fetch(url):
        seen.append(url)
        if 'pypi.org' in url:
            return {'info': {'version': '0.10.3'}}
        raise hs.Unavailable('unreachable')
    found = hs.release_check({}, NOW, fetch)
    assert found == {'checked_at': NOW, 'server': '0.10.3', 'client': None}
    assert hs.release_check(found, '2026-10-24T11:00:00+00:00', fetch) == found
    assert len(seen) == 2


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'http://host', 'https://user:pass@host',
                                 'https://host/?token=secret', None])
def test_web_url_never_contains_credentials_or_executable_scheme(url):
    assert hs.public_url(url) is None


def test_atomic_snapshot_and_invalid_files(tmp_path):
    target = tmp_path / 'status.json'
    hs.write_snapshot(target, {'collected_at': NOW})
    assert hs.read_json(target) == {'collected_at': NOW}
    assert list(tmp_path.iterdir()) == [target]
    target.write_text('not json')
    assert hs.read_json(target) == {}
    assert hs.read_json(tmp_path) == {}


def test_api_access_freshness_project_bindings_and_week_window(tmp_path, monkeypatch):
    from web_env import WebEnv
    from kd.cabinet.web import hindsight
    w = WebEnv(tmp_path)
    w.users.create('owner', 'Owner', roles=[('owner', '*')], actor='root')
    w.users.create('anna', 'Anna', roles=[('partner', ['web'])], actor='root')
    snap = tmp_path / 'status.json'
    monkeypatch.setattr(hindsight, 'SNAPSHOT', snap)
    # The collector writes ISO with a UTC offset and fractional seconds; freshness must accept it.
    timestamp = datetime.strptime(w.env.now, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    hs.write_snapshot(snap, {**hs.collect(fake_api, [], {}, now=timestamp.isoformat(timespec='microseconds')),
                             'clients': [{'agent': 'dev', 'bank': 'coding-agent::dev'}]})
    ledger = tmp_path / 'usage.jsonl'
    w.svc.usage_path = ledger
    today = timestamp.date()
    from datetime import timedelta
    ledger.write_text('\n'.join(json.dumps({'day': (today + timedelta(days=offset)).isoformat(),
                                           'agent': 'hindsight', 'category': 'hindsight', 'coverage': 'none'})
                                for offset in (-8, -6, -1, 0, 1)))
    assert w.client.get('/api/hindsight').status_code == 401
    w.login('anna')
    assert w.client.get('/api/hindsight').status_code == 403
    w.login()
    response = w.client.get('/api/hindsight')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['stale'] is False
    assert len(data['usage']) == 3
    assert all(p['bank'] is None for p in data['projects'])
    assert data['banks'][0]['agents'] == ['dev']
    w.svc._now = lambda: '2026-10-24T22:30:00Z'
    period = w.client.get('/api/hindsight').json()['period']
    assert period == {'start': '2026-10-18', 'end': '2026-10-24', 'timezone': 'UTC'}  # the example host
    monkeypatch.setattr(clock, 'zone', lambda: ZoneInfo('Etc/GMT-3'))  # the ledger's days are the host's
    period = w.client.get('/api/hindsight').json()['period']
    assert period == {'start': '2026-10-19', 'end': '2026-10-25', 'timezone': 'Etc/GMT-3'}
    snap.unlink()
    data = w.client.get('/api/hindsight').json()
    assert data['stale'] is True and data['banks'] == []


def test_a_banks_projects_and_a_project_bank_missing_on_the_server(tmp_path, monkeypatch):
    """a project bound to a bank shows it; the bank lists its projects beside its agents; a bound bank the
    server does not have yet is listed as missing, never created."""
    from web_env import WebEnv
    from kd.cabinet.web import hindsight
    w = WebEnv(tmp_path)
    w.users.create('owner', 'Owner', roles=[('owner', '*')], actor='root')
    snap = tmp_path / 'status.json'
    monkeypatch.setattr(hindsight, 'SNAPSHOT', snap)
    hs.write_snapshot(snap, {**hs.collect(fake_api, [], {}, now=w.env.now),
                             'clients': [{'agent': 'dev', 'bank': 'coding-agent::dev'}]})
    w.env.store.update_project('digipaddock', actor='root', memory_bank='coding-agent::dev')
    w.env.store.update_project('web', actor='root', memory_bank='coding-agent::shop')
    w.login()
    data = w.client.get('/api/hindsight').json()
    projects = {p['id']: p['bank'] for p in data['projects']}
    assert projects['digipaddock'] == 'coding-agent::dev' and projects['web'] == 'coding-agent::shop'
    assert all(bank is None for pid, bank in projects.items() if pid not in ('digipaddock', 'web'))
    banks = {b['id']: b for b in data['banks']}
    assert (banks['coding-agent::dev']['agents'], banks['coding-agent::dev']['projects']) == (['dev'], ['digipaddock'])
    assert banks['coding-agent::shop']['missing'] is True and banks['coding-agent::shop']['projects'] == ['web']
