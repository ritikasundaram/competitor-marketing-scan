import { useEffect, useMemo, useState } from "react";
import {
  fetchReport,
  getHealth,
  listReports,
  resumeScan,
  startScan,
  type Company,
  type Health,
  type Question,
  type ReportListItem,
  type Rival,
  type SavedReport,
  type ScanEvent,
  type Settings,
} from "./api";
import Dashboard from "./Dashboard";
import { Card, Chip, StatusChip } from "./ui";

type Phase = "setup" | "running" | "rivals" | "questions" | "report";

const STEPS: { node: string; label: string; optional?: boolean }[] = [
  { node: "understand", label: "Understand your company" },
  { node: "find_rivals", label: "Find rivals" },
  { node: "confirm_rivals", label: "You confirm rivals" },
  { node: "plan_questions", label: "Break into small questions" },
  { node: "approve_questions", label: "You approve questions", optional: true },
  { node: "research", label: "Research in parallel" },
  { node: "review_gaps", label: "Review gaps" },
  { node: "event_followups", label: "Dig into each event" },
  { node: "profile_rival", label: "Profile each rival" },
  { node: "plan_actions", label: "Write the action plan" },
  { node: "summarize_events", label: "Summarize the findings" },
  { node: "write_report", label: "Save the report" },
];

const DEFAULT_SETTINGS: Settings = {
  mock: false,
  max_rivals: 4,
  questions_per_rival: 4,
  own_questions: 2,
  max_age_days: 365,
  approve_questions: false,
  focus: "general",
  planner_model: "",
  researcher_model: "",
};

type QState = { q: Question; status: string; query?: string; findings?: number; attempts?: number };

export default function App() {
  const [phase, setPhase] = useState<Phase>("setup");
  const [company, setCompany] = useState<Company>({ name: "", domain: "", description: "", goal: "", rivals: [] });
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS);
  const [health, setHealth] = useState<Health | null>(null);
  const [reports, setReports] = useState<ReportListItem[]>([]);
  const [error, setError] = useState("");

  const [runId, setRunId] = useState("");
  const [message, setMessage] = useState("");
  const [doneNodes, setDoneNodes] = useState<string[]>([]);
  const [questions, setQuestions] = useState<Record<string, QState>>({});
  const [gapNote, setGapNote] = useState("");
  const [profiling, setProfiling] = useState<Record<string, string>>({});
  const [pendingRivals, setPendingRivals] = useState<(Rival & { keep: boolean })[]>([]);
  const [pendingQuestions, setPendingQuestions] = useState<Question[]>([]);
  const [report, setReport] = useState<SavedReport | null>(null);

  useEffect(() => {
    getHealth().then(setHealth).catch(() => setHealth(null));
    listReports().then(setReports).catch(() => {});
    const run = new URLSearchParams(window.location.search).get("run");
    if (run) openReport(run);
  }, []);

  async function openReport(id: string) {
    try {
      setError("");
      setReport(await fetchReport(id));
      setPhase("report");
      window.history.replaceState(null, "", `?run=${id}`);
      window.scrollTo({ top: 0 });
    } catch (e) {
      setError(`Couldn't open report ${id}: ${(e as Error).message}`);
    }
  }

  function onEvent(e: ScanEvent) {
    switch (e.event) {
      case "step":
        setMessage(e.message);
        break;
      case "node":
        setDoneNodes((d) => (d.includes(e.node) ? d : [...d, e.node]));
        break;
      case "questions":
        setQuestions((qs) => {
          const next = e.follow_up ? { ...qs } : {};
          for (const q of e.questions) next[q.id] = { q, status: "queued" };
          return next;
        });
        if (e.follow_up && e.gaps) setGapNote(e.gaps);
        break;
      case "question":
        setQuestions((qs) =>
          qs[e.id] ? { ...qs, [e.id]: { ...qs[e.id], status: e.status, query: e.query ?? qs[e.id].query, findings: e.findings, attempts: e.attempts } } : qs,
        );
        if (e.status === "researching") setMessage("Researchers are searching with Exa…");
        break;
      case "notice":
        setMessage(e.message);
        break;
      case "rival":
        setProfiling((p) => ({ ...p, [e.name]: e.status }));
        break;
      case "interrupt":
        setRunId(e.run_id);
        if (e.kind === "confirm_rivals") {
          setPendingRivals(e.rivals.map((r) => ({ ...r, keep: true })));
          setPhase("rivals");
        } else {
          setPendingQuestions(e.questions);
          setPhase("questions");
        }
        break;
      case "done":
        listReports().then(setReports).catch(() => {});
        openReport(e.run_id);
        break;
      case "error":
        setError(e.message);
        setPhase("setup");
        break;
    }
  }

  async function run(fn: () => Promise<void>) {
    setError("");
    setPhase("running");
    try {
      await fn();
    } catch (e) {
      setError((e as Error).message);
      setPhase("setup");
    }
  }

  function start() {
    setDoneNodes([]);
    setQuestions({});
    setGapNote("");
    setProfiling({});
    setReport(null);
    setMessage("Starting…");
    window.history.replaceState(null, "", "/");
    const rivals = company.rivals.filter((r) => r.name.trim());
    run(() => startScan({ ...company, rivals }, settings, onEvent));
  }

  function resume(value: unknown) {
    run(() => resumeScan(runId, value, onEvent));
  }

  const missingKeys = health && !settings.mock && (!health.has_exa_key || !health.has_ai_key);

  return (
    <div className="mx-auto max-w-6xl px-4 py-6 sm:px-6 print:max-w-none print:p-0">
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3 print:hidden">
        <button onClick={() => { setPhase("setup"); window.history.replaceState(null, "", "/"); }} className="text-left">
          <h1 className="text-xl font-semibold tracking-tight">Competitor Scan</h1>
          <p className="text-sm text-slate-500">An agent that researches your rivals with Exa and tells you what to do next.</p>
        </button>
        <div className="flex w-full min-w-0 flex-wrap items-center gap-2 sm:w-auto">
          {health && (
            <>
              <Chip tone={health.has_exa_key ? "green" : "amber"}>Exa key {health.has_exa_key ? "✓" : "missing"}</Chip>
              <Chip tone={health.has_ai_key ? "green" : "amber"}>{aiName(health)} key {health.has_ai_key ? "✓" : "missing"}</Chip>
              <a href={health.phoenix.url} target="_blank" rel="noreferrer">
                <Chip tone={health.phoenix.up ? "green" : "slate"}>Phoenix {health.phoenix.up ? "running ↗" : "off"}</Chip>
              </a>
            </>
          )}
          {reports.length > 0 && (
            <select
              className="w-full max-w-full rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm sm:w-64"
              value=""
              onChange={(e) => e.target.value && openReport(e.target.value)}
            >
              <option value="">Saved reports ({reports.length})</option>
              {reports.map((r) => (
                <option key={r.run_id} value={r.run_id}>
                  {r.company} vs {r.rivals.join(", ")} · {r.created.slice(0, 10)}
                  {r.focus === "field" ? " · field" : ""}
                  {r.mock ? " · mock" : ""}
                </option>
              ))}
            </select>
          )}
        </div>
      </header>

      {error && (
        <div role="alert" className="mb-4 rounded-lg border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-900 print:hidden">
          {error}
        </div>
      )}

      {phase === "setup" && (
        <Setup company={company} setCompany={setCompany} settings={settings} setSettings={setSettings} onStart={start}
          health={health} missingKeys={!!missingKeys} />
      )}

      {(phase === "running" || phase === "rivals" || phase === "questions") && (
        <div className="grid gap-4 lg:grid-cols-[260px_1fr]">
          <Progress doneNodes={doneNodes} phase={phase} approve={settings.approve_questions} field={settings.focus === "field"} />
          <div className="space-y-4">
            {phase === "rivals" && <ConfirmRivals rivals={pendingRivals} setRivals={setPendingRivals} onContinue={resume} />}
            {phase === "questions" && (
              <ApproveQuestions questions={pendingQuestions} setQuestions={setPendingQuestions} onContinue={resume} />
            )}
            {phase === "running" && (
              <Live message={message} questions={questions} gapNote={gapNote} profiling={profiling} />
            )}
          </div>
        </div>
      )}

      {phase === "report" && report && <Dashboard report={report} />}
    </div>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="text-sm font-medium text-slate-800">{label}</span>
      {hint && <span className="ml-2 text-xs text-slate-500">{hint}</span>}
      <div className="mt-1">{children}</div>
    </label>
  );
}

const input = "w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-200";

function Setup(props: {
  company: Company;
  setCompany: (c: Company) => void;
  settings: Settings;
  setSettings: (s: Settings) => void;
  onStart: () => void;
  health: Health | null;
  missingKeys: boolean;
}) {
  const { company: c, setCompany, settings: s, setSettings } = props;
  const set = (patch: Partial<Company>) => setCompany({ ...c, ...patch });
  const setS = (patch: Partial<Settings>) => setSettings({ ...s, ...patch });
  const est = s.max_rivals * s.questions_per_rival + s.own_questions + (s.focus === "field" ? s.max_rivals * 4 : 0);
  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <Card>
        <h2 className="mb-4 text-base font-semibold">Your company</h2>
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name">
            <input className={input} value={c.name} onChange={(e) => set({ name: e.target.value })} placeholder="Figma" />
          </Field>
          <Field label="Website" hint="so look-alikes stay out">
            <input className={input} value={c.domain} onChange={(e) => set({ domain: e.target.value })} placeholder="figma.com" />
          </Field>
          <div className="sm:col-span-2">
            <Field label="What it does" hint="optional">
              <input className={input} value={c.description} onChange={(e) => set({ description: e.target.value })}
                placeholder="Collaborative design tool for product teams" />
            </Field>
          </div>
          <div className="sm:col-span-2">
            <Field label="What do you want to learn?" hint="optional, steers the questions">
              <textarea className={`${input} min-h-20`} value={c.goal} onChange={(e) => set({ goal: e.target.value })}
                placeholder="How rivals are marketing to enterprise teams, and where we should respond this quarter" />
            </Field>
          </div>
          <div className="sm:col-span-2">
            <Field label="Rivals you already know" hint="optional; leave empty and the agent finds them">
              <RivalInputs rivals={c.rivals} setRivals={(rivals) => set({ rivals })} />
            </Field>
          </div>
        </div>
        <div className="mt-6 flex flex-wrap items-center gap-3">
          <button
            onClick={props.onStart}
            disabled={!c.name.trim() || props.missingKeys}
            className="rounded-lg bg-blue-700 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-800 disabled:cursor-not-allowed disabled:bg-slate-300"
          >
            Run the agent
          </button>
          {props.missingKeys && (
            <span className="text-sm text-amber-800">
              Add {!props.health?.has_exa_key && "EXA_API_KEY"}
              {!props.health?.has_exa_key && !props.health?.has_ai_key && " and "}
              {!props.health?.has_ai_key && props.health?.ai_key_name} to .env, or tick Mock mode.
            </span>
          )}
        </div>
      </Card>

      <Card className="space-y-4">
        <h2 className="text-base font-semibold">Settings</h2>
        <fieldset>
          <legend className="text-sm font-medium">Focus</legend>
          <div className="mt-1 grid grid-cols-2 gap-1 rounded-lg bg-slate-100 p-1" role="radiogroup">
            {([["general", "All marketing"], ["field", "Field marketing"]] as const).map(([v, label]) => (
              <button key={v} type="button" role="radio" aria-checked={s.focus === v} onClick={() => setS({ focus: v })}
                className={`rounded-md px-2 py-1.5 text-sm ${s.focus === v ? "bg-white font-semibold shadow-sm" : "text-slate-600 hover:text-slate-900"}`}>
                {label}
              </button>
            ))}
          </div>
          <p className="mt-1 text-xs text-slate-500">
            {s.focus === "field"
              ? "Rivals' events: conferences, meetups, roadshows, sponsorships. Then attendance and social engagement for the main ones."
              : "Launches, campaigns, channels, partnerships, pricing and more."}
          </p>
        </fieldset>
        <Toggle checked={s.mock} onChange={(mock) => setS({ mock })} label="Mock mode"
          hint="Fake web and fake AI. Free, for trying it out." />
        <Toggle checked={s.approve_questions} onChange={(v) => setS({ approve_questions: v })} label="Approve questions"
          hint="Pause so you can edit the question list before research starts." />
        <Toggle checked disabled onChange={() => {}} label="Confirm rivals" hint="Always on: untick anyone who isn't a real rival." />
        <div className="grid grid-cols-2 gap-3">
          <Field label="Rivals">
            <input type="number" min={1} max={6} className={input} value={s.max_rivals}
              onChange={(e) => setS({ max_rivals: clamp(+e.target.value, 1, 6) })} />
          </Field>
          <Field label={s.focus === "field" ? "Event questions each" : "Questions each"}>
            <input type="number" min={1} max={8} className={input} value={s.questions_per_rival}
              onChange={(e) => setS({ questions_per_rival: clamp(+e.target.value, 1, 8) })} />
          </Field>
          <Field label="About you">
            <input type="number" min={0} max={4} className={input} value={s.own_questions}
              onChange={(e) => setS({ own_questions: clamp(+e.target.value, 0, 4) })} />
          </Field>
          <Field label="Max age (days)">
            <input type="number" min={0} max={3650} className={input} value={s.max_age_days}
              onChange={(e) => setS({ max_age_days: clamp(+e.target.value, 0, 3650) })} />
          </Field>
        </div>
        <details className="text-sm">
          <summary className="cursor-pointer text-slate-600">AI models{props.health ? ` (${aiName(props.health)})` : ""}</summary>
          <div className="mt-3 space-y-3">
            <Field label="Planner" hint="judgment steps">
              <input className={input} value={s.planner_model} onChange={(e) => setS({ planner_model: e.target.value })}
                placeholder={props.health?.models.planner ?? "claude-haiku-5-5"} />
            </Field>
            <Field label="Researchers" hint="run many times">
              <input className={input} value={s.researcher_model} onChange={(e) => setS({ researcher_model: e.target.value })}
                placeholder={props.health?.models.researcher ?? "claude-haiku-5-5"} />
            </Field>
          </div>
        </details>
        <p className="text-xs text-slate-500">
          About {est} questions, each 1–2 Exa searches plus page reads
          {s.focus === "field" ? ", including attendance and engagement questions for 2 events per rival." : ", and up to 4 follow-ups."}
        </p>
      </Card>
    </div>
  );
}

function aiName(h: Health) {
  return h.ai_name;
}

function clamp(n: number, lo: number, hi: number) {
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, Math.round(n))) : lo;
}

function Toggle(props: { checked: boolean; onChange: (v: boolean) => void; label: string; hint: string; disabled?: boolean }) {
  return (
    <label className={`flex items-start gap-3 ${props.disabled ? "opacity-60" : "cursor-pointer"}`}>
      <input type="checkbox" className="mt-1 h-4 w-4 accent-blue-700" checked={props.checked} disabled={props.disabled}
        onChange={(e) => props.onChange(e.target.checked)} />
      <span>
        <span className="block text-sm font-medium">{props.label}</span>
        <span className="block text-xs text-slate-500">{props.hint}</span>
      </span>
    </label>
  );
}

function RivalInputs({ rivals, setRivals }: { rivals: Rival[]; setRivals: (r: Rival[]) => void }) {
  const update = (i: number, patch: Partial<Rival>) => setRivals(rivals.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div className="space-y-2">
      {rivals.map((r, i) => (
        <div key={i} className="flex gap-2">
          <input className={input} value={r.name} placeholder="Name" onChange={(e) => update(i, { name: e.target.value })} />
          <input className={input} value={r.domain} placeholder="website.com" onChange={(e) => update(i, { domain: e.target.value })} />
          <button className="px-2 text-slate-400 hover:text-rose-600" aria-label="Remove rival"
            onClick={() => setRivals(rivals.filter((_, j) => j !== i))}>✕</button>
        </div>
      ))}
      <button className="text-sm text-blue-700 hover:underline" onClick={() => setRivals([...rivals, { name: "", domain: "" }])}>
        + Add a rival
      </button>
    </div>
  );
}

function Progress({ doneNodes, phase, approve, field }: { doneNodes: string[]; phase: Phase; approve: boolean; field: boolean }) {
  const skip = field ? ["review_gaps", "plan_actions"] : ["event_followups", "summarize_events"];
  const steps = STEPS.filter((s) => (!s.optional || approve) && !skip.includes(s.node));
  const current = steps.find((s, i) => !steps.slice(i).some((x) => doneNodes.includes(x.node)) || (
    (s.node === "research" || s.node === "profile_rival") && !steps.slice(i + 1).some(
      (x) => doneNodes.includes(x.node) && !(s.node === "research" && x.node === "event_followups"))));
  return (
    <Card className="h-fit">
      <h2 className="mb-3 text-sm font-semibold text-slate-700">Agent steps</h2>
      <ol className="space-y-2 text-sm">
        {steps.map((s, i) => {
          // Parallel steps (many researchers, many profiles) finish when the next step starts.
          // In field focus, "dig into each event" starts a second research round, so it doesn't end research.
          const later = steps.slice(i + 1).some((x) => doneNodes.includes(x.node) && !(s.node === "research" && x.node === "event_followups"));
          const parallel = s.node === "research" || s.node === "profile_rival";
          const done = later || (!parallel && doneNodes.includes(s.node));
          const active = current?.node === s.node;
          const waiting = active && (phase === "rivals" || phase === "questions");
          return (
            <li key={s.node} className="flex items-center gap-2">
              <span aria-hidden className={`grid h-5 w-5 place-items-center rounded-full text-[11px] ${
                done ? "bg-emerald-600 text-white" : waiting ? "bg-amber-500 text-white" : active ? "bg-blue-600 text-white animate-pulse" : "bg-slate-200 text-slate-500"}`}>
                {done ? "✓" : waiting ? "!" : ""}
              </span>
              <span className={done ? "text-slate-500" : active ? "font-medium" : "text-slate-400"}>
                {s.label}
                {waiting && <span className="ml-1 text-amber-700">· waiting for you</span>}
              </span>
            </li>
          );
        })}
      </ol>
    </Card>
  );
}

function Live({ message, questions, gapNote, profiling }: {
  message: string; questions: Record<string, QState>; gapNote: string; profiling: Record<string, string>;
}) {
  const list = Object.values(questions);
  const byCompany = useMemo(() => {
    const m = new Map<string, QState[]>();
    for (const q of list) m.set(q.q.company, [...(m.get(q.q.company) ?? []), q]);
    return [...m.entries()];
  }, [list]);
  const finished = list.filter((q) => ["answered", "partial", "unanswered"].includes(q.status)).length;
  return (
    <>
      <Card>
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm font-medium" aria-live="polite">{message}</p>
          {list.length > 0 && <span className="text-sm text-slate-500">{finished} of {list.length} questions done</span>}
        </div>
        {list.length > 0 && (
          <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-slate-100">
            <div className="h-full rounded-full bg-blue-600 transition-all" style={{ width: `${(finished / list.length) * 100}%` }} />
          </div>
        )}
        {gapNote && <p className="mt-3 text-sm text-violet-800">Next round: {gapNote}</p>}
        {Object.keys(profiling).length > 0 && (
          <p className="mt-2 text-sm text-slate-600">
            Profiling: {Object.entries(profiling).map(([n, s]) => `${n} ${s === "done" ? "✓" : "…"}`).join(" · ")}
          </p>
        )}
      </Card>
      {byCompany.map(([name, qs]) => (
        <Card key={name}>
          <h3 className="mb-3 text-sm font-semibold">{name}{qs[0].q.is_self ? " (you)" : ""}</h3>
          <ul className="space-y-2">
            {qs.map(({ q, status, query, findings }) => (
              <li key={q.id} className="flex flex-col gap-1 rounded-lg border border-slate-100 p-3 sm:flex-row sm:items-start sm:justify-between">
                <div className="min-w-0">
                  <p className="text-sm"><span className="font-mono text-xs text-slate-400">{q.id}</span> {q.question}</p>
                  {status === "retrying" && query && <p className="mt-1 text-xs text-violet-700">New search: {query}</p>}
                  {q.round > 1 && <p className="mt-1 text-xs text-violet-700">Follow-up from the gap review</p>}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {findings !== undefined && <span className="text-xs text-slate-500">{findings} findings</span>}
                  <StatusChip status={status} />
                </div>
              </li>
            ))}
          </ul>
        </Card>
      ))}
    </>
  );
}

function ConfirmRivals({ rivals, setRivals, onContinue }: {
  rivals: (Rival & { keep: boolean })[]; setRivals: (r: (Rival & { keep: boolean })[]) => void; onContinue: (v: Rival[]) => void;
}) {
  const [name, setName] = useState("");
  const [domain, setDomain] = useState("");
  const kept = rivals.filter((r) => r.keep);
  return (
    <Card>
      <h2 className="text-base font-semibold">Are these your rivals?</h2>
      <p className="mb-4 text-sm text-slate-500">Untick anyone who isn't a direct competitor. You can add one too.</p>
      <ul className="space-y-2">
        {rivals.map((r, i) => (
          <li key={r.name + i}>
            <label className="flex cursor-pointer items-start gap-3 rounded-lg border border-slate-200 p-3 hover:bg-slate-50">
              <input type="checkbox" className="mt-1 h-4 w-4 accent-blue-700" checked={r.keep}
                onChange={(e) => setRivals(rivals.map((x, j) => (j === i ? { ...x, keep: e.target.checked } : x)))} />
              <span>
                <span className="block text-sm font-medium">{r.name} <span className="font-normal text-slate-500">{r.domain}</span></span>
                {r.reason && <span className="block text-xs text-slate-500">{r.reason}</span>}
              </span>
            </label>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <input className={input} placeholder="Add a rival: name" value={name} onChange={(e) => setName(e.target.value)} />
        <input className={input} placeholder="website.com" value={domain} onChange={(e) => setDomain(e.target.value)} />
        <button className="rounded-lg border border-slate-300 px-3 text-sm hover:bg-slate-50 disabled:opacity-50" disabled={!name.trim()}
          onClick={() => { setRivals([...rivals, { name: name.trim(), domain: domain.trim(), reason: "added by you", keep: true }]); setName(""); setDomain(""); }}>
          Add
        </button>
      </div>
      <button onClick={() => onContinue(kept.map(({ keep: _k, ...r }) => r))} disabled={!kept.length}
        className="mt-5 rounded-lg bg-blue-700 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-800 disabled:bg-slate-300">
        Research {kept.length} rival{kept.length === 1 ? "" : "s"}
      </button>
    </Card>
  );
}

function ApproveQuestions({ questions, setQuestions, onContinue }: {
  questions: Question[]; setQuestions: (q: Question[]) => void; onContinue: (v: Question[]) => void;
}) {
  const companies = [...new Set(questions.map((q) => q.company))];
  const [addFor, setAddFor] = useState(companies[0] ?? "");
  const [text, setText] = useState("");
  const update = (i: number, question: string) =>
    setQuestions(questions.map((q, j) =>
      j === i ? { ...q, question, search_query: `${q.company}${q.domain ? ` (${q.domain})` : ""} ${question}` } : q));
  return (
    <Card>
      <h2 className="text-base font-semibold">Approve the questions</h2>
      <p className="mb-4 text-sm text-slate-500">Each one goes to its own researcher. Edit, remove or add before research starts.</p>
      <ul className="space-y-2">
        {questions.map((q, i) => (
          <li key={q.id + i} className="flex items-start gap-2">
            <Chip>{q.company}</Chip>
            <textarea className={`${input} min-h-10 flex-1`} rows={1} value={q.question} onChange={(e) => update(i, e.target.value)} />
            <button className="px-2 pt-2 text-slate-400 hover:text-rose-600" aria-label={`Remove ${q.id}`}
              onClick={() => setQuestions(questions.filter((_, j) => j !== i))}>✕</button>
          </li>
        ))}
      </ul>
      <div className="mt-3 flex flex-col gap-2 sm:flex-row">
        <select className={`${input} sm:w-44`} value={addFor} onChange={(e) => setAddFor(e.target.value)}>
          {companies.map((c) => <option key={c}>{c}</option>)}
        </select>
        <input className={input} placeholder="Add a question" value={text} onChange={(e) => setText(e.target.value)} />
        <button className="rounded-lg border border-slate-300 px-3 text-sm hover:bg-slate-50 disabled:opacity-50" disabled={!text.trim()}
          onClick={() => {
            const ref = questions.find((q) => q.company === addFor);
            setQuestions([...questions, {
              id: "", company: addFor, domain: ref?.domain ?? "", is_self: ref?.is_self ?? false, dimension: "campaigns",
              question: text.trim(), search_query: `${addFor}${ref?.domain ? ` (${ref.domain})` : ""} ${text.trim()}`, round: 1,
            }]);
            setText("");
          }}>
          Add
        </button>
      </div>
      <button onClick={() => onContinue(questions.map((q) => ({ ...q, search_query: q.search_query || q.question })))}
        disabled={!questions.length}
        className="mt-5 rounded-lg bg-blue-700 px-4 py-2 text-sm font-semibold text-white hover:bg-blue-800 disabled:bg-slate-300">
        Research {questions.length} question{questions.length === 1 ? "" : "s"}
      </button>
    </Card>
  );
}
