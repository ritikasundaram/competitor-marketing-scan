import { useEffect, useMemo, useState } from "react";
import {
  discover,
  fetchReport,
  listRuns,
  scan,
  type Competitor,
  type SavedReport,
  type SavedRun,
} from "./api";
import Dashboard from "./Dashboard";

type Step = 1 | 2 | 3;

export default function App() {
  const [step, setStep] = useState<Step>(1);
  const [company, setCompany] = useState({ name: "", domain: "", description: "" });
  const [mock, setMock] = useState(false);
  const [grade, setGrade] = useState(true);
  const [tracer, setTracer] = useState("none");
  const [maxCompetitors, setMaxCompetitors] = useState(4);
  const [maxQueries, setMaxQueries] = useState(3);
  const [fetchProof, setFetchProof] = useState(true);
  const [synthesis, setSynthesis] = useState("auto");

  const [found, setFound] = useState<Competitor[]>([]);
  const [kept, setKept] = useState<boolean[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [progress, setProgress] = useState("");
  const [report, setReport] = useState<SavedReport | null>(null);
  const [runs, setRuns] = useState<SavedRun[]>([]);
  const [savedRunId, setSavedRunId] = useState("");

  const selected = useMemo(() => found.filter((_, i) => kept[i]), [found, kept]);

  useEffect(() => {
    listRuns().then(setRuns).catch(() => {});
  }, []);

  // A report can be opened from the address bar: /?run=<id>
  useEffect(() => {
    const run = new URLSearchParams(window.location.search).get("run");
    if (run) {
      setSavedRunId(run);
      openReport(run);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function openReport(runId: string) {
    try {
      setReport(await fetchReport(runId));
      window.history.replaceState(null, "", `?run=${runId}`);
      setTimeout(() => document.getElementById("report")?.scrollIntoView({ behavior: "smooth" }), 50);
    } catch (e) {
      setError(String(e));
    }
  }

  function onLoadSaved(runId: string) {
    setSavedRunId(runId);
    if (runId) openReport(runId);
  }

  async function onFind() {
    if (!company.name.trim()) return setError("Company name is required.");
    setError("");
    setBusy("Discovering competitors…");
    try {
      const list = await discover(company, { mock, max_competitors: maxCompetitors });
      setFound(list);
      setKept(list.map(() => true));
      setStep(2);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy("");
    }
  }

  function onRun() {
    setError("");
    setReport(null);
    setSavedRunId("");
    setBusy("Scanning…");
    scan(
      {
        company,
        competitors: selected,
        mock,
        grade,
        tracer,
        max_competitors: maxCompetitors,
        max_queries_per_competitor: maxQueries,
        fetch_proof: fetchProof,
        max_proof_urls: 5,
        synthesis,
      },
      (e) => {
        if (e.event === "competitor_done")
          setProgress(`Scanned ${e.name} (${e.done}/${e.total})…`);
        else if (e.event === "self_scan") setProgress(`Checking ${e.name}'s own recent campaigns…`);
        else if (e.event === "discovered")
          setProgress(`Discovered ${(e.competitors as unknown[] | undefined)?.length ?? 0} rivals…`);
      },
      (d) => {
        setBusy("");
        setProgress("");
        setStep(3);
        setSavedRunId(d.run_id);
        openReport(d.run_id);
        listRuns().then(setRuns).catch(() => {});
      },
      (msg) => {
        setError(msg);
        setBusy("");
      },
    );
  }

  return (
    <div className="mx-auto max-w-6xl px-6 py-8">
      <header className="mb-8 print:hidden">
        <h1 className="text-3xl font-bold tracking-tight">🔍 Competitor Marketing Scan</h1>
        <p className="mt-1 text-sm text-slate-500">
          Exa web search · rivals auto-discovered · effectiveness is public proxies, never ROI.
        </p>
        <ol className="mt-4 flex gap-2 text-sm">
          {["Company", "Confirm rivals", "Results"].map((label, i) => (
            <li
              key={label}
              className={`rounded-full px-3 py-1 font-medium ${
                step === (i + 1 as Step) ? "bg-slate-900 text-white" : "bg-slate-200 text-slate-600"
              }`}
            >
              {i + 1}. {label}
            </li>
          ))}
        </ol>
      </header>

      <details className="mb-6 rounded-2xl border border-slate-200 bg-white p-4 text-sm shadow-sm print:hidden">
        <summary className="cursor-pointer font-medium text-slate-600">
          Saved reports ({runs.length})
        </summary>
        <select
          className="mt-3 rounded-lg border border-slate-300 px-3 py-1.5"
          value={savedRunId}
          onChange={(e) => onLoadSaved(e.target.value)}
        >
          <option value="">Select a run…</option>
          {runs.map((r) => (
            <option key={r.run_id} value={r.run_id}>
              {r.date} · {r.company || "?"} · {r.run_id}
              {r.has_data ? "" : " (text only)"}
            </option>
          ))}
        </select>
      </details>

      {error && <div className="mb-4 rounded-lg bg-rose-50 p-3 print:hidden text-sm text-rose-700 ring-1 ring-rose-200">{error}</div>}

      {step >= 1 && (
        <section className="mb-6 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm print:hidden">
          <h2 className="mb-4 text-lg font-semibold">① Your company</h2>
          <div className="grid gap-4 md:grid-cols-2">
            <label className="block text-sm font-medium">
              Company name *
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2"
                placeholder="Figma"
                value={company.name}
                onChange={(e) => setCompany({ ...company, name: e.target.value })}
              />
            </label>
            <label className="block text-sm font-medium">
              Domain
              <input
                className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2"
                placeholder="figma.com"
                value={company.domain}
                onChange={(e) => setCompany({ ...company, domain: e.target.value })}
              />
            </label>
          </div>
          <label className="mt-4 block text-sm font-medium">
            One-line description
            <input
              className="mt-1 w-full rounded-lg border border-slate-300 px-3 py-2"
              placeholder="Collaborative design tool"
              value={company.description}
              onChange={(e) => setCompany({ ...company, description: e.target.value })}
            />
          </label>
          <details className="mt-4 text-sm">
            <summary className="cursor-pointer font-medium text-slate-600">Settings</summary>
            <div className="mt-3 grid gap-3 md:grid-cols-2">
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={mock} onChange={(e) => setMock(e.target.checked)} />
                Mock mode (free, no Exa cost)
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={grade} onChange={(e) => setGrade(e.target.checked)} />
                Auto-grade campaigns (Exa)
              </label>
              <label className="flex items-center gap-2">
                Trace backend
                <select className="rounded-lg border border-slate-300 px-2 py-1" value={tracer} onChange={(e) => setTracer(e.target.value)}>
                  {["none", "phoenix", "langfuse", "both"].map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-2">
                Max competitors: {maxCompetitors}
                <input type="range" min={1} max={6} value={maxCompetitors} onChange={(e) => setMaxCompetitors(+e.target.value)} />
              </label>
              <label className="flex items-center gap-2">
                Queries per rival: {maxQueries}
                <input type="range" min={1} max={6} value={maxQueries} onChange={(e) => setMaxQueries(+e.target.value)} />
              </label>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={fetchProof} onChange={(e) => setFetchProof(e.target.checked)} />
                Fetch page proof (Exa contents)
              </label>
              <label className="flex items-center gap-2">
                Synthesis
                <select className="rounded-lg border border-slate-300 px-2 py-1" value={synthesis} onChange={(e) => setSynthesis(e.target.value)}>
                  <option value="auto">Auto (OpenAI if key)</option>
                  <option value="template">Template ($0)</option>
                  <option value="openai">OpenAI</option>
                </select>
              </label>
            </div>
          </details>
          <button
            onClick={onFind}
            disabled={!!busy}
            className="mt-4 rounded-lg bg-slate-900 px-5 py-2 font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {busy || "Find competitors"}
          </button>
        </section>
      )}

      {step >= 2 && found.length > 0 && (
        <section className="mb-6 rounded-2xl border border-slate-200 bg-white p-6 shadow-sm print:hidden">
          <h2 className="mb-1 text-lg font-semibold">② Confirm rivals</h2>
          <p className="mb-4 text-sm text-slate-500">Untick anything that isn't a direct rival before spending scan credits.</p>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            {found.map((c, i) => (
              <label
                key={i}
                className={`cursor-pointer rounded-xl border p-4 ${kept[i] ? "border-slate-900 bg-slate-50" : "border-slate-200 opacity-60"}`}
              >
                <input
                  type="checkbox"
                  className="mb-2"
                  checked={kept[i]}
                  onChange={() => setKept(kept.map((k, j) => (j === i ? !k : k)))}
                />
                <div className="font-semibold">{c.name}</div>
                <div className="text-xs text-slate-500">{c.domain}</div>
                <div className="mt-1 text-xs italic text-slate-500">{c.reason?.slice(0, 120)}</div>
              </label>
            ))}
          </div>
          <button
            onClick={onRun}
            disabled={!!busy || selected.length === 0}
            className="mt-4 rounded-lg bg-slate-900 px-5 py-2 font-semibold text-white hover:bg-slate-700 disabled:opacity-50"
          >
            {busy || `Run scan on ${selected.length} competitor${selected.length === 1 ? "" : "s"}`}
          </button>
          {progress && <p className="mt-2 text-sm text-slate-500">{progress}</p>}
        </section>
      )}

      {report && <Dashboard report={report} />}
    </div>
  );
}
