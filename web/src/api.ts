// Types and calls for the agentic scan API. Keys never reach the browser.

export type Rival = { name: string; domain: string; reason?: string };

export type Question = {
  id: string;
  company: string;
  domain: string;
  is_self: boolean;
  dimension: string;
  question: string;
  search_query: string;
  round: number;
};

export type Finding = {
  claim: string;
  source_id: string;
  date: string;
  confidence: "High" | "Medium" | "Low";
  flags: string[];
};

export type Answer = {
  question: Question;
  status: "answered" | "partial" | "unanswered";
  answer: string;
  findings: Finding[];
  attempts: number;
  queries: string[];
  missing: string;
};

export type Source = {
  url: string;
  title: string;
  date: string;
  company: string;
  question_id: string;
  excerpt: string;
};

export type Campaign = {
  name: string;
  what: string;
  channel: string;
  strength: "Strong" | "Medium" | "Weak";
  why: string;
  source_ids: string[];
  rubric_note: string;
};

export type Profile = {
  rival: Rival;
  summary: string;
  positioning: string;
  campaigns: Campaign[];
  threat: "High" | "Medium" | "Low";
  threat_why: string;
};

export type Level = "Low" | "Medium" | "High";

export type Action = {
  title: string;
  what: string;
  why: string;
  first_step: string;
  channel: string;
  effort: Level;
  impact: Level;
  horizon: string;
  source_ids: string[];
};

export type Dropped = {
  claim?: string;
  source_id?: string;
  reason: string;
  question_id?: string;
};

export type ReportData = {
  version: 2;
  run_id: string;
  created: string;
  company: { name: string; domain: string; description?: string; goal?: string };
  profile: { summary: string; market: string; audiences: string[]; products: string[]; focus: string };
  rivals: Rival[];
  plan: { headline: string; actions: Action[]; dropped: { title: string; reason: string }[] };
  profiles: Profile[];
  questions: Answer[];
  sources: Record<string, Source>;
  dropped: Dropped[];
  reviews: { round: number; gaps: string; added: number }[];
  settings: Record<string, unknown> & { mock?: boolean; max_age_days?: number };
  stats: {
    questions: number;
    answered: number;
    partial: number;
    unanswered: number;
    retried: number;
    follow_ups: number;
    findings: number;
    sources: number;
    flagged_numbers: number;
    undated: number;
    dropped: Record<string, number>;
    lowered_by_rubric: number;
    actions: number;
    actions_dropped: number;
  };
  trace: { session_id: string; trace_id: string };
};

export type SavedReport = { data: ReportData; markdown: string; filename: string; phoenix_url: string };

export type ReportListItem = {
  run_id: string;
  created: string;
  company: string;
  rivals: string[];
  mock: boolean;
  actions: number;
};

export type Health = {
  has_exa_key: boolean;
  has_ai_key: boolean;
  models: { planner: string; researcher: string };
  phoenix: { url: string; up: boolean };
};

export type Settings = {
  mock: boolean;
  max_rivals: number;
  questions_per_rival: number;
  own_questions: number;
  max_age_days: number;
  approve_questions: boolean;
  planner_model: string;
  researcher_model: string;
};

export type Company = { name: string; domain: string; description: string; goal: string; rivals: Rival[] };

// Events streamed while a scan runs.
export type ScanEvent =
  | { event: "step"; step: string; message: string }
  | { event: "node"; node: string }
  | { event: "questions"; questions: Question[]; follow_up?: boolean; gaps?: string }
  | {
      event: "question";
      id: string;
      status: "researching" | "retrying" | "answered" | "partial" | "unanswered";
      query?: string;
      attempts?: number;
      findings?: number;
    }
  | { event: "rival"; name: string; status: "profiling" | "done" }
  | { event: "interrupt"; run_id: string; kind: "confirm_rivals"; rivals: Rival[]; trace_id: string }
  | { event: "interrupt"; run_id: string; kind: "approve_questions"; questions: Question[]; trace_id: string }
  | { event: "done"; run_id: string; trace_id: string; report: { run_id: string; file: string } }
  | { event: "error"; message: string };

async function json<T>(res: Response): Promise<T> {
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {
      /* keep status text */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

export const getHealth = () => fetch("/api/health").then((r) => json<Health>(r));
export const listReports = () =>
  fetch("/api/reports").then((r) => json<{ reports: ReportListItem[] }>(r)).then((b) => b.reports);
export const fetchReport = (runId: string) => fetch(`/api/reports/${runId}`).then((r) => json<SavedReport>(r));

async function stream(url: string, body: unknown, onEvent: (e: ScanEvent) => void): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) {
    await json(res);
    return;
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let cut: number;
    while ((cut = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, cut);
      buf = buf.slice(cut + 2);
      for (const line of chunk.split("\n")) {
        if (line.startsWith("data: ")) onEvent(JSON.parse(line.slice(6)) as ScanEvent);
      }
    }
  }
}

export const startScan = (company: Company, settings: Settings, onEvent: (e: ScanEvent) => void) =>
  stream("/api/scans", { company, settings }, onEvent);

export const resumeScan = (runId: string, value: unknown, onEvent: (e: ScanEvent) => void) =>
  stream(`/api/scans/${runId}/resume`, { value }, onEvent);

export const phoenixLink = (base: string, data: ReportData) =>
  data.trace.trace_id ? `${base.replace(/\/$/, "")}/redirects/traces/${data.trace.trace_id}` : base;
