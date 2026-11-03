export interface Competitor {
  name: string;
  domain: string;
  reason: string;
}

export interface Campaign {
  competitor: string;
  title: string;
  url: string;
  date: string;
  channel: string;
  message: string;
  signal: string;
  strength: string;
  confidence: string;
  why: string;
  snippet?: string;
  proof?: string;
  flags?: string[];
  excluded?: string;
}

export interface ScanDone {
  run_id: string;
  flat: Campaign[];
  old?: Campaign[];
  report: string;
  tracer?: string;
  otel_trace_id?: string;
}

export interface SavedRun {
  run_id: string;
  date: string;
  filename: string;
  company?: string;
  has_data?: boolean;
}

export async function discover(
  company: { name: string; domain: string; description: string },
  opts: { mock: boolean; max_competitors: number },
): Promise<Competitor[]> {
  const res = await fetch("/api/discover", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ...company, ...opts }),
  });
  if (!res.ok) throw new Error((await res.json()).detail ?? "discovery failed");
  return (await res.json()).competitors;
}

export function scan(
  body: {
    company: { name: string; domain: string; description: string };
    competitors: Competitor[];
    mock: boolean;
    grade: boolean;
    tracer: string;
    max_competitors: number;
    max_queries_per_competitor: number;
    fetch_proof: boolean;
    max_proof_urls: number;
    synthesis: string;
  },
  onEvent: (e: Record<string, unknown>) => void,
  onDone: (d: ScanDone) => void,
  onError: (msg: string) => void,
): () => void {
  const ctrl = new AbortController();
  (async () => {
    try {
      const res = await fetch("/api/scan", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify(body),
        signal: ctrl.signal,
      });
      if (!res.ok || !res.body) throw new Error("scan request failed");
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const p of parts) {
          const line = p.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const evt = JSON.parse(line.slice(5)) as Record<string, unknown>;
          if (evt.event === "done") onDone(evt as unknown as ScanDone);
          else if (evt.event === "error") onError(String(evt.message));
          else onEvent(evt);
        }
      }
    } catch (err) {
      if ((err as Error).name !== "AbortError") onError(String(err));
    }
  })();
  return () => ctrl.abort();
}

export async function listRuns(): Promise<SavedRun[]> {
  const res = await fetch("/api/runs");
  if (!res.ok) throw new Error("could not list saved runs");
  return (await res.json()).runs;
}

export interface ReportData {
  run_id: string;
  date: string;
  my_company: { name: string; domain?: string; description?: string };
  competitors: Record<string, Omit<Campaign, "competitor">[]>;
  recommendations?: {
    title: string;
    action: string;
    why: string;
    evidence: { competitor: string; title: string; url: string }[];
    flags?: string[];
  }[];
  strong: { competitor: string; title: string; url: string; date?: string; why?: string }[];
  top_channels: { channel: string; count: number }[];
  excluded: Campaign[];
  own_campaigns?: Omit<Campaign, "competitor">[];
  checks: {
    max_age_days: number;
    excluded_old: number;
    excluded_off_topic: number;
    undated: number;
    unverified_numbers: number;
  };
  method: string[];
  tracer?: string;
  otel_trace_id?: string;
  mock?: boolean;
}

export interface SavedReport {
  run_id: string;
  filename: string;
  markdown: string;
  data: ReportData | null; // null for reports saved before the dashboard existed
}

export async function fetchReport(runId: string): Promise<SavedReport> {
  const res = await fetch(`/api/reports/${runId}`);
  if (!res.ok) throw new Error((await res.json()).detail ?? "report not found");
  return res.json();
}
