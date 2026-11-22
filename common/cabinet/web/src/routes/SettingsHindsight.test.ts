import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, expect, it, vi } from 'vitest';
import { session } from '../lib/session.svelte';
import { releaseState, type HindsightStatus } from '../lib/hindsight';
import { match, parentOf } from '../lib/router.svelte';
import SettingsHindsight from './SettingsHindsight.svelte';

afterEach(() => { cleanup(); session.clear(); vi.unstubAllGlobals(); });
const login = (allowed = true, memory = true) => session.signedIn({ id: 'owner', name: 'Owner', language: 'ru',
  timezone: 'Europe/Lisbon', permissions: allowed ? { '*': ['settings'] } : {}, projects: [], csrf: 'x', push_public_key: '',
  second_factor: { passkey: false, totp: false, password: false }, must_setup: false, memory });

it('on a host without Hindsight says so and asks the server nothing (KD-9192)', async () => {
  login(true, false);
  const fetch = vi.fn(() => Promise.resolve(Response.json(data)));
  vi.stubGlobal('fetch', fetch);
  render(SettingsHindsight);
  expect(screen.getByText(/Hindsight на этом хосте не установлен/)).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Обновить данные' })).toBeNull();
  expect(fetch).not.toHaveBeenCalled();
});
const data: HindsightStatus = {
  stale: false, collected_at: new Date().toISOString(), server: { state: 'online', version: '0.9.2', audit_log: false },
  banks_complete: true, units: { 'hindsight-worker': 'active' }, releases: { server: '0.10.2', client: '0.8.1' },
  clients: [{ agent: 'dev', bank: 'coding-agent::dev', version: '0.8.0', enabled: true, configured: true,
    policy: { autoUpdate: false, retainSessions: false, gitIngest: 'message' } }],
  banks: [{ id: 'coding-agent::dev', name: null, agents: ['dev'], stats: { total_documents: 10, failed_operations: 2 }, activity: null }],
  projects: [{ id: 'app', name: 'Application', route: '@dev', bank: null }], usage: [],
  period: { start: '2026-10-18', end: '2026-10-24', timezone: 'Etc/GMT-3' },
};

it('shows actual banks, missing project bindings, unknown audit counts and software updates', async () => {
  login();
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(Response.json(data))));
  render(SettingsHindsight);
  expect(await screen.findByText('Доступен')).toBeTruthy();
  expect(screen.getByText('Application')).toBeTruthy();
  expect(screen.getByText('Память роли')).toBeTruthy();
  expect(screen.getAllByText('Есть новая версия')).toHaveLength(2);
  expect(screen.getByText(/Журнал обращений выключен/)).toBeTruthy();
  expect(screen.getByText(/Адрес веб-интерфейса не настроен/)).toBeTruthy();
  expect(screen.getByText('Данные о расходе за этот период отсутствуют')).toBeTruthy();
  expect(screen.queryByRole('link', { name: 'Открыть веб-интерфейс' })).toBeNull();
  await fireEvent.click(screen.getByRole('button', { name: 'Обновить данные' }));
  expect(fetch).toHaveBeenCalledTimes(2);
});

it('shows a bank with its projects and a bound project with its bank (KD-9183)', async () => {
  login();
  const bound: HindsightStatus = {
    ...data,
    banks: [{ ...data.banks[0], projects: ['app'] },
      { id: 'coding-agent::shop', name: null, agents: [], projects: ['shop'], stats: null, activity: null, missing: true }],
    projects: [{ id: 'app', name: 'Application', route: '@dev', bank: 'coding-agent::dev' },
      { id: 'shop', name: 'Shop', route: '@owner', bank: 'coding-agent::shop' },
      { id: 'ops', name: 'Ops', route: '@sysadmin', bank: null }],
  };
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(Response.json(bound))));
  render(SettingsHindsight);
  expect(await screen.findByText('Shop')).toBeTruthy();
  const shopBank = screen.getByRole('heading', { name: 'coding-agent::shop' }).closest('article')!;
  expect(shopBank.textContent).toContain('Проект: shop');
  expect(shopBank.textContent).toContain('Нет привязанных агентов фабрики');
  const row = screen.getByText('Ops').closest('tr')!;
  expect(row.textContent).toContain('Память роли');
  expect(screen.getByText('Shop').closest('tr')!.textContent).toContain('coding-agent::shop');
  expect(screen.queryByText(/Привязка памяти к проектам не настроена/)).toBeNull();
});

it('marks an old snapshot unknown and offers the configured control plane link', async () => {
  login();
  vi.stubGlobal('fetch', vi.fn(() => Promise.resolve(Response.json({ ...data, stale: true, web_url: 'https://memory.example.com' }))));
  render(SettingsHindsight);
  expect(await screen.findByText(/Данные устарели/)).toBeTruthy();
  expect(screen.queryByText('Доступен')).toBeNull();
  expect(screen.getByRole('link', { name: 'Открыть веб-интерфейс' }).getAttribute('href')).toBe('https://memory.example.com');
});

it('does not request diagnostics without settings permission', () => {
  login(false);
  vi.stubGlobal('fetch', vi.fn());
  render(SettingsHindsight);
  expect(fetch).not.toHaveBeenCalled();
});

it('routes back to settings and compares numeric stable releases', () => {
  expect(parentOf(match('/settings/hindsight'))).toBe('/settings');
  expect(releaseState('0.9.2', '0.10.0')).toBe('update');
  expect(releaseState('1.0.0', '0.10.0')).toBe('current');
  expect(releaseState(null, '0.10.0')).toBe('unknown');
  expect(releaseState('1.1.0rc1', '1.0.0')).toBe('unknown');
});
