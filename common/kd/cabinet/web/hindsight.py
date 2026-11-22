"""Hindsight settings, only with settings permission. Reads the collector's public snapshot; no network."""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request

from kd import clock
from kd.hindsight_status import SNAPSHOT, read_json
from kd.cabinet import users
from kd.cabinet.web import deps
from kd.cabinet.web.agents import _usage
from kd.cabinet.web.cards import _settings, call

router = APIRouter()


@router.get('/api/hindsight')
async def status(request: Request, user: deps.User = Depends(deps.current_user)):
    _settings(request, user)
    service = deps.svc(request)
    snapshot = read_json(SNAPSHOT)
    collected = snapshot.get('collected_at')
    try:
        age = (users.parse_time(service.now()) - datetime.fromisoformat(collected.replace('Z', '+00:00'))).total_seconds()
        stale = age < -60 or age > 900
    except (ValueError, TypeError, AttributeError):
        stale = True
    # a project's memory bank is the bank of its cards' sessions; routing a project to an agent is not
    # memory ownership, so a project without one shows unbound (its cards use the role bank)
    projects = [{'id': p['id'], 'name': p['name'], 'route': p['route'], 'bank': p.get('memory_bank') or None}
                for p in (await call(request, user, {'op': 'project.list'}))['items']]
    clients = snapshot.get('clients', [])
    banks = snapshot.get('banks', [])
    # Include configured banks (an agent's role bank, a project's bank) even when absent on the server. Never create
    # one to diagnose it.
    known = {b['id'] for b in banks}
    for configured in [c.get('bank') for c in clients] + [p['bank'] for p in projects]:
        if configured and configured not in known:
            banks.append({'id': configured, 'name': None, 'stats': None, 'activity': None, 'missing': True})
            known.add(configured)
    for bank in banks:
        bank['agents'] = [c['agent'] for c in clients if c.get('bank') == bank['id']]
        bank['projects'] = [p['id'] for p in projects if p['bank'] == bank['id']]
    # kd-usage ledger day labels are the host's (host.json timezone), independent of the cabinet timezone.
    end = users.parse_time(service.now()).astimezone(clock.zone()).date().isoformat()
    start = (date.fromisoformat(end) - timedelta(days=6)).isoformat()
    usage = [r for r in _usage(service)
             if r.get('category') == 'hindsight' and start <= r['day'] <= end]
    return {**snapshot, 'stale': stale, 'banks': banks, 'clients': clients, 'usage': usage,
            'period': {'start': start, 'end': end, 'timezone': str(clock.zone())}, 'projects': projects}
