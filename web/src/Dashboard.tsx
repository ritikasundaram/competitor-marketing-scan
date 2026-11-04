import { useEffect, useMemo, useState } from "react";
import { phoenixLink, type Action, type Answer, type Level, type ReportData, type RivalEvent, type SavedReport } from "./api";
import { Card, Chip, SourceLinks, StatusChip, StrengthPill, ThreatChip } from "./ui";

const RUBRIC = [
  ["Strong", "Covered by two or more independent outside sources, or a concrete result a source states (signups, attendance, ranking, reach)."],
  ["Medium", "One outside source, or clear engagement on the company's own channels."],
  ["Weak", "Only the company's own announcement, with no sign of pickup."],
] as const;

export default function Dashboard({ report }: { report: SavedReport }) {
  const d = report.data;
  const st = d.stats;
  const removed = Object.values(st.dropped).reduce((a, b) => a + b, 0);
  const field = d.focus === "field";

  // Print / Save as PDF: open every fold-out and name the file after the scan.
  useEffect(() => {
    const title = document.title;
    const opened: HTMLDetailsElement[] = [];
    const before = () => {
      document.title = report.filename.replace(/\.md$/, "");
      document.querySelectorAll<HTMLDetailsElement>("#report details:not([open])").forEach((el) => {
        el.open = true;
        opened.push(el);
      });
    };
    const after = () => {
      document.title = title;
      opened.splice(0).forEach((el) => (el.open = false));
    };
    window.addEventListener("beforeprint", before);
    window.addEventListener("afterprint", after);
    return () => {
      window.removeEventListener("beforeprint", before);
      window.removeEventListener("afterprint", after);
    };
  }, [report.filename]);

  function download() {
    const url = URL.createObjectURL(new Blob([report.markdown], { type: "text/markdown" }));
    const a = Object.assign(document.createElement("a"), { href: url, download: report.filename });
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <article id="report" className="space-y-6">
      <header className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <p className="text-sm text-slate-500">
            {field ? "Field marketing scan" : "Competitor scan"} · {d.created.slice(0, 10)} · run{" "}
            <span className="font-mono">{d.run_id}</span>
          </p>
          <h2 className="text-2xl font-semibold tracking-tight">
            {d.company.name} <span className="font-normal text-slate-400">vs</span> {d.rivals.map((r) => r.name).join(", ")}
          </h2>
        </div>
        <div className="flex flex-wrap gap-2 print:hidden">
          <button onClick={download} className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50">
            Download .md
          </button>
          <button onClick={() => window.print()} className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50">
            Print / save PDF
          </button>
          <a href={phoenixLink(report.phoenix_url, d)} target="_blank" rel="noreferrer"
            className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50">
            Open trace in Phoenix ↗
          </a>
        </div>
      </header>

      {d.settings.mock && (
        <p className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          Mock data: a fake web and a fake AI, with planted mistakes to show the checks working. Not real research.
        </p>
      )}

      <Card className="print-keep border-blue-200 bg-blue-50/40">
        <p className="text-xs font-semibold uppercase tracking-wide text-blue-800">Headline</p>
        <p className="mt-1 text-lg font-medium text-slate-900">{d.plan.headline}</p>
      </Card>

      {field ? (
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
          <Tile label="Rival events found" value={st.events ?? 0} />
          <Tile label="With attendance figures" value={st.events_with_attendance ?? 0} note="each backed by a source" />
          <Tile label="With social engagement" value={st.events_with_engagement ?? 0} note="from LinkedIn, X, YouTube…" />
          <Tile label="Numbers removed" value={st.event_metrics_flagged ?? 0} note="no source showed them" />
          <Tile label="Actions" value={st.actions} />
        </div>
      ) : (
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        <Tile label="Actions" value={st.actions} />
        <Tile label="Questions answered" value={`${st.answered}/${st.questions}`} note={st.retried ? `${st.retried} needed a 2nd search` : undefined} />
        <Tile label="Findings kept" value={st.findings} note={`from ${st.sources} sources`} />
        <Tile label="Removed by checks" value={removed} note={Object.entries(st.dropped).map(([k, n]) => `${n} ${k}`).join(", ") || undefined} />
        <Tile label="Ratings lowered" value={st.lowered_by_rubric} note="to fit the rubric" />
      </div>
      )}

      <ActionPlan d={d} />
      {field && <Events d={d} />}
      <Rivals d={d} />
      <Questions d={d} />
      <Checks d={d} />

      <footer className="border-t border-slate-200 pt-4 text-xs text-slate-500">
        Phoenix session <span className="font-mono">{d.trace.session_id}</span>
        {d.trace.trace_id && <> · trace <span className="font-mono">{d.trace.trace_id.slice(0, 12)}…</span></>}
        {" · "}Public signals only: never revenue, spend or ROI.
      </footer>
    </article>
  );
}

function Tile({ label, value, note }: { label: string; value: string | number; note?: string }) {
  return (
    <div className="print-keep rounded-xl border border-slate-200 bg-white p-4">
      <p className="text-xs text-slate-500">{label}</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
      {note && <p className="mt-1 text-xs text-slate-500">{note}</p>}
    </div>
  );
}

const LEVELS: Level[] = ["Low", "Medium", "High"];

function ActionPlan({ d }: { d: ReportData }) {
  const actions = d.plan.actions;
  const [hover, setHover] = useState<number | null>(null);
  return (
    <section>
      <h3 className="mb-3 text-lg font-semibold">Action plan</h3>
      <div className="grid gap-4 lg:grid-cols-[1fr_300px]">
        <ol className="space-y-3">
          {actions.map((a, i) => (
            <li key={i} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)}
              className={`print-keep rounded-xl border bg-white p-4 transition ${hover === i ? "border-blue-400" : "border-slate-200"}`}>
              <ActionCard a={a} n={i + 1} d={d} />
            </li>
          ))}
          {!actions.length && <p className="text-sm text-slate-500">No action survived the citation check.</p>}
        </ol>
        <Card className="print-keep h-fit">
          <h4 className="text-sm font-semibold">Impact vs effort</h4>
          <p className="mb-3 text-xs text-slate-500">Top-left are quick wins. Numbers match the list.</p>
          <div className="grid grid-cols-[auto_repeat(3,1fr)] gap-1 text-xs">
            {[...LEVELS].reverse().map((impact) => (
              <Row key={impact} impact={impact} actions={actions} hover={hover} setHover={setHover} />
            ))}
            <span />
            {LEVELS.map((e) => <span key={e} className="pt-1 text-center text-slate-500">{e}</span>)}
          </div>
          <p className="mt-1 text-center text-xs text-slate-500">Effort →</p>
        </Card>
      </div>
    </section>
  );
}

function Row({ impact, actions, hover, setHover }: {
  impact: Level; actions: Action[]; hover: number | null; setHover: (n: number | null) => void;
}) {
  return (
    <>
      <span className="flex items-center pr-1 text-slate-500">{impact}</span>
      {LEVELS.map((effort) => {
        const quick = impact === "High" && effort === "Low";
        const here = actions.map((a, i) => ({ a, i })).filter(({ a }) => a.impact === impact && a.effort === effort);
        return (
          <div key={effort} className={`flex min-h-12 flex-wrap content-center justify-center gap-1 rounded-md p-1 ${quick ? "bg-emerald-50" : "bg-slate-50"}`}
            title={`${impact} impact, ${effort} effort`}>
            {here.map(({ a, i }) => (
              <span key={i} onMouseEnter={() => setHover(i)} onMouseLeave={() => setHover(null)} title={a.title}
                className={`grid h-6 w-6 place-items-center rounded-full text-[11px] font-semibold ${hover === i ? "bg-blue-700 text-white" : "bg-white text-slate-800 ring-1 ring-slate-300"}`}>
                {i + 1}
              </span>
            ))}
          </div>
        );
      })}
    </>
  );
}

function ActionCard({ a, n, d }: { a: Action; n: number; d: ReportData }) {
  const rivals = [...new Set(a.source_ids.map((id) => d.sources[id]?.company).filter(Boolean))];
  return (
    <>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h4 className="font-semibold"><span className="mr-2 text-slate-400">{n}.</span>{a.title}</h4>
        <div className="flex flex-wrap gap-1">
          <Chip tone={a.impact === "High" ? "green" : "slate"}>{a.impact} impact</Chip>
          <Chip>{a.effort} effort</Chip>
          <Chip tone="blue">{a.horizon}</Chip>
          <Chip>{a.channel}</Chip>
        </div>
      </div>
      <p className="mt-2 text-sm text-slate-800">{a.what}</p>
      <p className="mt-1 text-sm text-slate-600"><span className="font-medium text-slate-700">Why: </span>{a.why}</p>
      <p className="mt-1 text-sm text-slate-600"><span className="font-medium text-slate-700">First step: </span>{a.first_step}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        Responds to {rivals.join(", ") || "rivals"}: <SourceLinks ids={a.source_ids} sources={d.sources} />
      </div>
      {a.flags?.map((fl) => (
        <div key={fl} className="mt-2"><Chip tone="amber">⚠ {fl}: check before using</Chip></div>
      ))}
    </>
  );
}

function Rivals({ d }: { d: ReportData }) {
  const order = { High: 0, Medium: 1, Low: 2 };
  const profiles = [...d.profiles].sort((a, b) => order[a.threat] - order[b.threat]);
  return (
    <section>
      <h3 className="mb-3 text-lg font-semibold">Rivals</h3>
      <div className="grid gap-4 md:grid-cols-2">
        {profiles.map((p) => (
          <Card key={p.rival.name} className="print-keep">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div>
                <h4 className="font-semibold">{p.rival.name}</h4>
                <p className="text-xs text-slate-500">{p.rival.domain}</p>
              </div>
              <ThreatChip level={p.threat} />
            </div>
            <p className="mt-3 text-sm text-slate-800">{p.summary}</p>
            <p className="mt-2 text-sm text-slate-600"><span className="font-medium text-slate-700">Positioning: </span>{p.positioning}</p>
            <p className="mt-1 text-sm text-slate-600"><span className="font-medium text-slate-700">Why this threat: </span>{p.threat_why}</p>
            <ul className="mt-4 space-y-3 border-t border-slate-100 pt-3">
              {p.campaigns.map((c, i) => (
                <li key={i} className="text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <StrengthPill value={c.strength} note={c.rubric_note} />
                    <span className="font-medium">{c.name}</span>
                    <span className="text-xs text-slate-500">{c.channel}</span>
                  </div>
                  <p className="mt-1 text-slate-700">{c.what}</p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {c.why}
                    {c.rubric_note && <span className="text-violet-700"> Rubric: {c.rubric_note}.</span>}
                  </p>
                  <div className="mt-1"><SourceLinks ids={c.source_ids} sources={d.sources} /></div>
                </li>
              ))}
              {!p.campaigns.length && <li className="text-sm text-slate-500">No campaign had checked sources.</li>}
            </ul>
          </Card>
        ))}
      </div>
    </section>
  );
}

function Questions({ d }: { d: ReportData }) {
  const [company, setCompany] = useState("all");
  const [status, setStatus] = useState("all");
  const companies = useMemo(() => [...new Set(d.questions.map((a) => a.question.company))], [d]);
  const list = d.questions.filter(
    (a) => (company === "all" || a.question.company === company) && (status === "all" || a.status === status),
  );
  const select = "rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm";
  return (
    <section>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <h3 className="text-lg font-semibold">Questions the agent asked</h3>
        <div className="flex gap-2 print:hidden">
          <select aria-label="Filter by company" className={select} value={company} onChange={(e) => setCompany(e.target.value)}>
            <option value="all">All companies</option>
            {companies.map((c) => <option key={c}>{c}</option>)}
          </select>
          <select aria-label="Filter by status" className={select} value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="all">Any status</option>
            <option value="answered">Answered</option>
            <option value="partial">Partly answered</option>
            <option value="unanswered">Not answered</option>
          </select>
        </div>
      </div>
      <div className="divide-y divide-slate-100 rounded-xl border border-slate-200 bg-white">
        {list.map((a) => <QuestionRow key={a.question.id} a={a} d={d} />)}
        {!list.length && <p className="p-4 text-sm text-slate-500">No questions match.</p>}
      </div>
    </section>
  );
}

function QuestionRow({ a, d }: { a: Answer; d: ReportData }) {
  const q = a.question;
  return (
    <details className="group p-4">
      <summary className="flex cursor-pointer list-none flex-col gap-2 sm:flex-row sm:items-start sm:justify-between">
        <span className="min-w-0 text-sm">
          <span className="mr-1 font-mono text-xs text-slate-400">{q.id}</span>
          <span className="font-medium">{q.company}</span>
          <span className="text-slate-400"> · {q.dimension} · </span>
          {q.question}
        </span>
        <span className="flex shrink-0 flex-wrap items-center gap-1">
          {q.round > 1 && <Chip tone="violet">follow-up</Chip>}
          {a.attempts > 1 && <Chip tone="violet">{a.attempts} searches</Chip>}
          <StatusChip status={a.status} />
        </span>
      </summary>
      <div className="mt-3 space-y-3 pl-0 text-sm sm:pl-8">
        {a.answer && <p className="text-slate-800">{a.answer}</p>}
        {a.findings.length > 0 && (
          <ul className="space-y-1.5">
            {a.findings.map((f, i) => (
              <li key={i} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-2">
                <span className="text-slate-700">• {f.claim}</span>
                <span className="flex flex-wrap items-center gap-1">
                  <SourceLinks ids={[f.source_id]} sources={d.sources} />
                  {f.date && <span className="text-xs text-slate-500">{f.date}</span>}
                  {f.flags.map((fl) => <Chip key={fl} tone="amber">⚠ {fl}</Chip>)}
                </span>
              </li>
            ))}
          </ul>
        )}
        {a.missing && a.status !== "answered" && <p className="text-xs text-slate-500">Still missing: {a.missing}</p>}
        <p className="text-xs text-slate-500">Searches: {a.queries.map((s) => `“${s}”`).join(" → ")}</p>
      </div>
    </details>
  );
}

function Checks({ d }: { d: ReportData }) {
  const groups = useMemo(() => {
    const m = new Map<string, ReportData["dropped"]>();
    for (const x of d.dropped) {
      const k = x.reason.startsWith("old") ? "Too old" : x.reason.includes("doesn't mention") ? "Wrong company"
        : x.reason.includes("isn't a source") ? "Made-up citation" : "Other";
      m.set(k, [...(m.get(k) ?? []), x]);
    }
    return [...m.entries()];
  }, [d]);
  const st = d.stats;
  return (
    <section className="grid gap-4 md:grid-cols-2">
      <Card>
        <h3 className="text-base font-semibold">Fact checks</h3>
        <p className="mt-1 text-sm text-slate-600">Plain rules, no AI. Every finding must cite a page the researcher read, be about the right company, and be recent.</p>
        <ul className="mt-3 space-y-1 text-sm">
          <li>⚠ {st.flagged_numbers} findings had numbers the source doesn't show (confidence set to Low)</li>
          <li>⚠ {st.undated} findings have no known date</li>
          <li>✕ {st.actions_dropped} actions dropped for citing no checked rival source</li>
          <li>⚠ {st.actions_flagged ?? 0} actions use numbers the sources don't show</li>
        </ul>
        {groups.map(([k, items]) => (
          <details key={k} className="mt-3 text-sm">
            <summary className="cursor-pointer font-medium">{k} ({items.length} removed)</summary>
            <ul className="mt-2 space-y-1 text-slate-600">
              {items.map((x, i) => (
                <li key={i}><span className="font-mono text-xs text-slate-400">{x.question_id}</span> {x.claim} <span className="text-slate-400">({x.reason})</span></li>
              ))}
            </ul>
          </details>
        ))}
        {d.plan.dropped.length > 0 && (
          <details className="mt-3 text-sm">
            <summary className="cursor-pointer font-medium">Dropped actions ({d.plan.dropped.length})</summary>
            <ul className="mt-2 space-y-1 text-slate-600">
              {d.plan.dropped.map((x, i) => <li key={i}>{x.title} <span className="text-slate-400">({x.reason})</span></li>)}
            </ul>
          </details>
        )}
      </Card>
      <Card>
        <h3 className="text-base font-semibold">How it was rated</h3>
        <dl className="mt-3 space-y-2 text-sm">
          {RUBRIC.map(([k, v]) => (
            <div key={k} className="flex gap-3"><dt className="w-20 shrink-0"><StrengthPill value={k} /></dt><dd className="text-slate-600">{v}</dd></div>
          ))}
        </dl>
        <p className="mt-3 text-xs text-slate-500">The AI rates first; a rule then lowers any rating its sources can't back. A ↓ marks a lowered rating.</p>
        <h4 className="mt-4 text-sm font-semibold">Gap review</h4>
        <ul className="mt-1 space-y-1 text-sm text-slate-600">
          {d.reviews.map((r) => (
            <li key={r.round}>Round {r.round}: {r.gaps || "no further round"}{r.added ? ` → ${r.added} follow-up question${r.added > 1 ? "s" : ""}` : ""}</li>
          ))}
        </ul>
        <h4 className="mt-4 text-sm font-semibold">About {d.company.name}</h4>
        <p className="mt-1 text-sm text-slate-600">{d.profile.summary}</p>
        {d.profile.products?.length > 0 && <p className="mt-1 text-xs text-slate-500">Already offers: {d.profile.products.join(", ")}</p>}
      </Card>
    </section>
  );
}

function Events({ d }: { d: ReportData }) {
  const [rival, setRival] = useState("all");
  const rows = d.profiles.flatMap((p) => (p.events ?? []).map((e) => ({ rival: p.rival.name, e })));
  const list = rows.filter((r) => rival === "all" || r.rival === rival);
  const where = (e: RivalEvent) => e.location || (e.format !== "unknown" ? e.format : "");
  const Metric = ({ value, flags, name }: { value: string; flags: string[]; name: string }) => {
    const flag = flags.find((f) => f.startsWith(name));
    if (value) return <span className="font-medium text-slate-900">{value}</span>;
    if (flag) return <span title={flag}><Chip tone="amber">⚠ removed: no source</Chip></span>;
    return <span className="text-slate-400">not found</span>;
  };
  return (
    <section>
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3 className="text-lg font-semibold">Rival events</h3>
          <p className="text-xs text-slate-500">Every figure is quoted from a source; a figure no source showed is removed and marked ⚠.</p>
        </div>
        <select aria-label="Filter events by rival" className="rounded-lg border border-slate-300 bg-white px-2 py-1 text-sm print:hidden"
          value={rival} onChange={(e) => setRival(e.target.value)}>
          <option value="all">All rivals</option>
          {d.profiles.map((p) => <option key={p.rival.name}>{p.rival.name}</option>)}
        </select>
      </div>
      {!list.length && <p className="rounded-xl border border-slate-200 bg-white p-4 text-sm text-slate-500">No events with checked sources.</p>}
      <div className="hidden overflow-hidden rounded-xl border border-slate-200 bg-white md:block print:block">
        <table className="w-full text-left text-sm">
          <thead className="bg-slate-50 text-xs text-slate-500">
            <tr>
              <th className="px-3 py-2 font-medium">Event</th>
              <th className="px-3 py-2 font-medium">When · where</th>
              <th className="px-3 py-2 font-medium">Attendees</th>
              <th className="px-3 py-2 font-medium">Social engagement</th>
              <th className="px-3 py-2 font-medium">Strength</th>
              <th className="px-3 py-2 font-medium">Sources</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-100">
            {list.map(({ rival: r, e }, i) => (
              <tr key={i} className="align-top">
                <td className="px-3 py-3">
                  <p className="font-medium">{e.name}</p>
                  <p className="text-xs text-slate-500">{r} · {e.kind}{e.audience ? ` · for ${e.audience}` : ""}</p>
                  {e.coverage && <p className="mt-1 text-xs text-slate-500">Coverage: {e.coverage}</p>}
                  {e.partners && <p className="text-xs text-slate-500">Partners: {e.partners}</p>}
                </td>
                <td className="px-3 py-3 text-slate-700">{e.date || "date unknown"}<br /><span className="text-xs text-slate-500">{where(e)}</span></td>
                <td className="px-3 py-3"><Metric value={e.attendees} flags={e.flags} name="attendees" /></td>
                <td className="px-3 py-3"><Metric value={e.engagement} flags={e.flags} name="engagement" /></td>
                <td className="px-3 py-3"><StrengthPill value={e.strength} note={e.rubric_note} /></td>
                <td className="px-3 py-3"><SourceLinks ids={e.source_ids} sources={d.sources} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {d.profiles.some((p) => p.dropped_events?.length) && (
        <details className="mt-3 text-sm">
          <summary className="cursor-pointer text-slate-600">
            Events left out ({d.profiles.reduce((n, p) => n + (p.dropped_events?.length ?? 0), 0)}): another company with a similar name, too old, or no checked source
          </summary>
          <ul className="mt-2 space-y-1 text-slate-600">
            {d.profiles.flatMap((p) => (p.dropped_events ?? []).map((x, i) => (
              <li key={p.rival.name + i}>{p.rival.name}: {x.name} <span className="text-slate-400">({x.reason})</span></li>
            )))}
          </ul>
        </details>
      )}
      <ul className="mt-3 space-y-3 md:hidden print:hidden">
        {list.map(({ rival: r, e }, i) => (
          <li key={i} className="rounded-xl border border-slate-200 bg-white p-4 text-sm">
            <div className="flex items-start justify-between gap-2">
              <div>
                <p className="font-medium">{e.name}</p>
                <p className="text-xs text-slate-500">{r} · {e.kind} · {e.date || "date unknown"}{where(e) ? ` · ${where(e)}` : ""}</p>
              </div>
              <StrengthPill value={e.strength} note={e.rubric_note} />
            </div>
            <dl className="mt-3 grid grid-cols-2 gap-2 text-xs">
              <div><dt className="text-slate-500">Attendees</dt><dd><Metric value={e.attendees} flags={e.flags} name="attendees" /></dd></div>
              <div><dt className="text-slate-500">Social engagement</dt><dd><Metric value={e.engagement} flags={e.flags} name="engagement" /></dd></div>
            </dl>
            <div className="mt-2"><SourceLinks ids={e.source_ids} sources={d.sources} /></div>
          </li>
        ))}
      </ul>
    </section>
  );
}
