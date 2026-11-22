import type { UsageRow } from './types';

export interface HindsightClient {
  agent: string; bank: string | null; version: string | null; configured: boolean; enabled: boolean;
  policy: { autoUpdate?: boolean; retainSessions?: boolean; gitIngest?: string; pageTriggerCron?: string;
    autoSeed?: boolean; codebaseSurvey?: boolean; pageTriggerType?: string; disabled?: boolean };
}
export interface HindsightBank {
  id: string; name: string | null; agents: string[]; projects?: string[]; missing?: boolean;
  stats: Record<string, number> | null; last_write?: string; activity: Record<string, number> | null;
  stats_error?: string; activity_error?: string;
}
export interface HindsightStatus {
  collected_at?: string; stale: boolean;
  server?: { state: string; version: string | null; error?: string; audit_log?: boolean };
  units?: Record<string, string>; banks_complete?: boolean; banks_error?: string;
  clients: HindsightClient[]; banks: HindsightBank[]; web_url?: string | null;
  releases?: { checked_at?: string; server?: string | null; client?: string | null };
  projects: { id: string; name: string; route: string; bank: string | null }[];
  usage: UsageRow[]; period: { start: string; end: string; timezone: string };
}

/** Only compare stable releases; unknown and prerelease versions are never declared up to date. */
export function releaseState(installed?: string | null, latest?: string | null): 'unknown' | 'current' | 'update' {
  const parse = (v?: string | null) => /^\d+\.\d+\.\d+$/.test(v ?? '') ? v!.split('.').map(Number) : null;
  const a = parse(installed), b = parse(latest);
  if (!a || !b) return 'unknown';
  for (let i = 0; i < 3; i++) {
    if (a[i] < b[i]) return 'update';
    if (a[i] > b[i]) return 'current';
  }
  return 'current';
}
