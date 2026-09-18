// Dashboard data layer — talks to the FastAPI orchestrator (v2), not SQLite directly.
// Set JOBAGENT_API_URL to point at the backend (default http://127.0.0.1:8077).
const API = process.env.JOBAGENT_API_URL || "http://127.0.0.1:8077";

export function apiBase(): string {
  return API;
}

// Browser-facing base URL, for fetches that run in the visitor's browser rather
// than on the server. These are different addresses: a server-side 127.0.0.1 means
// "this machine", which in a browser means the visitor's own laptop. Set
// PUBLIC_JOBAGENT_API_URL whenever the dashboard and API are not co-hosted
// (e.g. dashboard on Vercel, API on a VPS). Falls back to the server URL, which is
// correct for local development where both are on one machine.
export function publicApiBase(): string {
  return (
    import.meta.env.PUBLIC_JOBAGENT_API_URL ||
    process.env.PUBLIC_JOBAGENT_API_URL ||
    API
  );
}

// Server-side token, used only when the API has JOBAGENT_REQUIRE_AUTH_READS on.
// These fetches run in the SSR process, which has no browser session to borrow — so
// the dashboard carries a token of its own. Get it with `python scripts/api_token.py`.
// Unset is the normal case and costs nothing: the header is simply omitted.
const API_TOKEN = process.env.JOBAGENT_API_TOKEN || "";

function serverHeaders(): Record<string, string> {
  return API_TOKEN ? { Authorization: `Bearer ${API_TOKEN}` } : {};
}

async function getJSON(path: string): Promise<any> {
  const res = await fetch(`${API}${path}`, { headers: serverHeaders() });
  if (res.status === 401 || res.status === 403) {
    // Distinguishable on purpose: "the API is up and refusing me" is a different
    // problem from "the API is down", and the generic message sent people hunting
    // the wrong one.
    throw new Error(
      `API ${res.status} for ${path} — reads are authenticated on this instance. ` +
      `Set JOBAGENT_API_TOKEN for the dashboard (python scripts/api_token.py).`);
  }
  if (!res.ok) throw new Error(`API ${res.status} ${res.statusText} for ${path}`);
  return res.json();
}

export interface SourceHealth {
  source: string;
  last_ingest: string | null;
  hours_since: number | null;
  fetched: number | null;
  new: number | null;
}

export interface Health {
  last_ingest: string | null;
  hours_since_ingest: number | null;
  is_stale: boolean;
  stale_after_hours: number;
  recent_errors: number;
  last_error: { source: string | null; error: string | null; at: string } | null;
  sources: SourceHealth[];
}

export interface Stats {
  totalJobs: number;
  matches: number;
  strong: number;
  queue: number;              // strong ∧ unstashed — the morning number
  totalApps: number;
  lastIngest: string | null;
  bySource: { source: string; n: number }[];
  apps: { status: string; n: number }[];
  health: Health | null;
}

export async function getStats(): Promise<Stats> {
  const s = await getJSON("/stats");
  return {
    totalJobs: s.total_jobs ?? 0,
    matches: s.matches ?? 0,
    strong: s.strong_matches ?? 0,
    queue: s.queue ?? 0,
    totalApps: s.total_apps ?? 0,
    lastIngest: s.last_ingest ?? null,
    bySource: Object.entries(s.by_source ?? {}).map(([source, n]) => ({ source, n: n as number })),
    apps: s.apps ?? [],
    health: s.health ?? null,
  };
}

export interface MatchRow {
  id: string;
  title: string;
  company: string | null;
  location: string | null;
  is_remote: number;
  source: string;
  url: string | null;
  apply_url: string | null;
  posted_at: string | null;
  score: number;
  rationale: string;
  // Why this might not fit: level mismatch, missing must-have, exclusion, not remote.
  // Surfaced in the list so a stash decision does not need a click-through.
  gaps: string[];
  // Parsed from salary_text at ingest. null means "could not tell", never zero.
  salary_min: number | null;
  salary_max: number | null;
  salary_currency: string | null;
  salary_period: string | null;
  // Groups the same role seen on several boards. Not the row identity.
  cluster_key: string | null;
  // Which scorer produced `score`; llm_score survives a heuristic re-run.
  score_source?: string;
  llm_score?: number | null;
  // Per-job stash decision, joined by the store. null = live.
  triage_state: "dismissed" | "snoozed" | null;
  triage_snoozed_until: string | null;
  triage_note: string | null;
}

export interface MatchFilter {
  days?: number;
  location?: "remote" | "hybrid" | "any";
  q?: string;
  exclude?: string;   // comma-separated locations to drop
  include?: string;   // comma-separated locations to keep-only
  sources?: string;   // comma-separated source slugs to keep-only
  minSalary?: number; // annualised floor; rows with unknown pay are kept
  limit?: number;
  offset?: number;
}

export async function getMatches(f: MatchFilter = {}): Promise<MatchRow[]> {
  const p = new URLSearchParams();
  if (f.days) p.set("days", String(f.days));
  if (f.location) p.set("location", f.location);
  if (f.q) p.set("q", f.q);
  if (f.exclude) p.set("exclude", f.exclude);
  if (f.include) p.set("include", f.include);
  if (f.sources) p.set("sources", f.sources);
  if (f.minSalary) p.set("min_salary", String(f.minSalary));
  p.set("limit", String(f.limit ?? 50));
  if (f.offset) p.set("offset", String(f.offset));
  return (await getJSON(`/jobs?${p.toString()}`)).jobs;
}

export interface AppRow {
  id: string;
  title: string;
  company: string | null;
  status: string;
  apply_method: string;
  created_at: string;
  submitted_at: string | null;
  url: string | null;
  apply_url: string | null;
  // Legal next statuses, computed server-side so the transition map is never
  // duplicated here. Anything outside this set needs an audited correction.
  allowed_next: string[];
}

export async function getApplications(limit = 200): Promise<AppRow[]> {
  return (await getJSON(`/applications?limit=${limit}`)).applications;
}

export interface JobDetail extends MatchRow {
  description?: string;
  salary_text?: string | null;
  apply_email?: string | null;
  tags?: string;
}

export async function getJob(id: string): Promise<JobDetail> {
  return getJSON(`/job/${encodeURIComponent(id)}`);
}

export interface Analytics {
  total: number;
  by_status: Record<string, number>;
  by_source: { source: string; n: number }[];
  timeline: { day: string; n: number }[];
  submitted: number;
  interview: number;
  offer: number;
  rejected: number;
  response_rate: number;
  interview_rate: number;
  offer_rate: number;
}

export async function getAnalytics(): Promise<Analytics> {
  return getJSON("/analytics");
}

export const APPLICATION_STATUSES = [
  "matched", "drafting", "awaiting_approval", "submitted",
  "rejected", "interview", "offer", "skipped", "failed",
];

export interface Followup {
  id: string;
  title: string;
  company: string | null;
  submitted_at: string;
  days_waiting: number;
  apply_email: string | null;
  url: string | null;
  apply_url: string | null;
}

// Submitted applications that have gone quiet. Read-only; drafting a nudge is a
// separate, explicit action and nothing here can send mail.
export async function getFollowups(afterDays = 7): Promise<{ followups: Followup[]; after_days: number }> {
  return await getJSON(`/followups?after_days=${afterDays}`);
}

export interface SourceInfo {
  available: string[];              // everything selectable
  enabled: string[];                // what the next fetch will run
  in_store: Record<string, number>; // stored job count per source
}

// Public read — used by the Jobs source filter and the Settings source picker.
export async function getSources(): Promise<SourceInfo> {
  return await getJSON("/sources");
}
