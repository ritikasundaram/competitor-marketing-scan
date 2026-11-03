import { useMemo, useState } from "react";
import type { Campaign, SavedReport } from "./api";

// Local Phoenix UI (see README); scans traced there get a direct link.
const PHOENIX_URL = "http://localhost:6006";

// Strength is ordered, so it uses one blue ramp: darker = stronger.
// Checked with the dataviz palette validator (ordinal, on white).
const STRENGTHS = [
  { key: "Strong", color: "#184f95" },
  { key: "Medium", color: "#3987e5" },
  { key: "Weak", color: "#86b6ef" },
] as const;
const TBD_COLOR = "#d6d5d0";

function strengthOf(c: { strength?: string }) {
  const s = (c.strength ?? "").split(" ")[0];
  return STRENGTHS.some((x) => x.key === s) ? s : "TBD";
}

function strengthColor(s: string) {
  return STRENGTHS.find((x) => x.key === s)?.color ?? TBD_COLOR;
}

function topChannel(camps: { channel?: string }[]) {
  const counts: Record<string, number> = {};
  for (const c of camps) {
    const ch = (c.channel ?? "").split(",")[0].trim();
    if (ch) counts[ch] = (counts[ch] ?? 0) + 1;
  }
  return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0] ?? "—";
}

function StrengthTag({ value }: { value: string }) {
  const s = strengthOf({ strength: value });
  return (
    <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-700">
      <span className="h-2.5 w-2.5 rounded-full" style={{ background: strengthColor(s) }} />
      {s}
    </span>
  );
}

function Legend() {
  return (
    <div className="flex flex-wrap gap-4 text-xs text-slate-600">
      {[...STRENGTHS, { key: "TBD", color: TBD_COLOR }].map((s) => (
        <span key={s.key} className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-sm" style={{ background: s.color }} />
          {s.key}
        </span>
      ))}
    </div>
  );
}

/** One horizontal stacked bar: how a competitor's campaigns split by strength. */
function StrengthBar({ name, counts, max }: { name: string; counts: Record<string, number>; max: number }) {
  const [hover, setHover] = useState<string | null>(null);
  const keys = ["Strong", "Medium", "Weak", "TBD"].filter((k) => counts[k] > 0);
  const total = keys.reduce((n, k) => n + counts[k], 0);
  return (
    <div className="relative flex h-5 items-center" style={{ width: `${max ? (total / max) * 100 : 0}%`, minWidth: total ? 24 : 0 }}>
      {keys.map((k, i) => (
        <div
          key={k}
          onMouseEnter={() => setHover(k)}
          onMouseLeave={() => setHover(null)}
          className="h-3.5 cursor-default"
          style={{
            flexGrow: counts[k],
            background: strengthColor(k),
            marginLeft: i ? 2 : 0, // 2px gap between segments
            borderTopRightRadius: i === keys.length - 1 ? 4 : 0,
            borderBottomRightRadius: i === keys.length - 1 ? 4 : 0,
            opacity: hover && hover !== k ? 0.45 : 1,
          }}
        />
      ))}
      {hover && (
        <div className="pointer-events-none absolute -top-9 left-0 z-10 whitespace-nowrap rounded-md bg-slate-900 px-2 py-1 text-xs text-white shadow">
          {name}: {counts[hover]} {hover} of {total}
        </div>
      )}
    </div>
  );
}

function Tile({ label, value, note }: { label: string; value: number; note?: string }) {
  return (
    <div className="rounded-xl bg-white p-4 ring-1 ring-slate-200">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className="mt-1 text-3xl font-semibold text-slate-900">{value}</div>
      {note && <div className="mt-1 text-xs text-slate-500">{note}</div>}
    </div>
  );
}

function download(filename: string, text: string, type: string) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = filename;
  a.click();
  URL.revokeObjectURL(a.href);
}

export default function Dashboard({ report }: { report: SavedReport }) {
  const { data, markdown, filename } = report;
  const [query, setQuery] = useState("");
  const [strength, setStrength] = useState("All");
  const [competitor, setCompetitor] = useState("All");

  const campaigns: Campaign[] = useMemo(
    () =>
      Object.entries(data?.competitors ?? {}).flatMap(([name, camps]) =>
        camps.map((c) => ({ ...c, competitor: name }) as Campaign),
      ),
    [data],
  );

  const perCompetitor = useMemo(
    () =>
      Object.entries(data?.competitors ?? {}).map(([name, camps]) => {
        const counts: Record<string, number> = { Strong: 0, Medium: 0, Weak: 0, TBD: 0 };
        for (const c of camps) counts[strengthOf(c)] += 1;
        return { name, total: camps.length, counts, channel: topChannel(camps) };
      }),
    [data],
  );
  const maxTotal = Math.max(0, ...perCompetitor.map((p) => p.total));

  const rows = useMemo(() => {
    const q = query.toLowerCase();
    return campaigns.filter(
      (c) =>
        (strength === "All" || strengthOf(c) === strength) &&
        (competitor === "All" || c.competitor === competitor) &&
        (!q || `${c.title} ${c.signal} ${c.message} ${c.competitor}`.toLowerCase().includes(q)),
    );
  }, [campaigns, query, strength, competitor]);

  const actions = (
    <div className="ml-auto flex gap-2 print:hidden">
      <button
        onClick={() => download(filename, markdown, "text/markdown")}
        className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-sm font-medium hover:bg-slate-50"
      >
        Download .md
      </button>
      <button
        onClick={() => window.print()}
        className="rounded-lg bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700"
      >
        Print / save PDF
      </button>
    </div>
  );

  // Reports saved before the dashboard existed only have markdown.
  if (!data) {
    return (
      <section id="report" className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <h2 className="text-lg font-semibold">Report {report.run_id}</h2>
          {actions}
        </div>
        <p className="mb-3 text-xs text-slate-500">
          This report was saved before the dashboard view existed, so only the text version is available. Re-run the
          scan to get the dashboard.
        </p>
        <pre className="max-h-[32rem] overflow-auto whitespace-pre-wrap rounded-lg bg-slate-50 p-4 text-xs text-slate-700 ring-1 ring-slate-200">
          {markdown}
        </pre>
      </section>
    );
  }

  const recs = data.recommendations ?? [];
  const flagged = campaigns.filter((c) => (c.flags ?? []).length > 0).length;
  const strongCount = perCompetitor.reduce((n, p) => n + p.counts.Strong, 0);
  const phoenix = data.otel_trace_id && ["phoenix", "both"].includes(data.tracer ?? "");

  return (
    <section id="report" className="space-y-5">
      {/* Header */}
      <div className="flex flex-wrap items-center gap-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div>
          <h2 className="text-xl font-semibold">{data.my_company.name} · competitor scan</h2>
          <p className="text-xs text-slate-500">
            {data.date} · run {data.run_id} · {perCompetitor.length} competitors
            {data.mock && " · mock data"}
          </p>
        </div>
        {actions}
      </div>

      {/* Headline numbers */}
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Tile label="Campaigns analyzed" value={campaigns.length} />
        <Tile label="Strong" value={strongCount} note="public signals only, not ROI" />
        <Tile label="Need a second look" value={flagged} note="unverified numbers or no date" />
        <Tile label="Excluded" value={data.excluded.length} note="too old or a different company" />
      </div>

      {/* Competitor comparison */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="mb-3 flex flex-wrap items-center gap-3">
          <h3 className="font-semibold">Campaign strength by competitor</h3>
          <div className="ml-auto">
            <Legend />
          </div>
        </div>
        <table className="w-full text-left text-sm">
          <thead className="text-xs text-slate-500">
            <tr>
              <th className="py-2 pr-4 font-medium">Competitor</th>
              <th className="py-2 pr-4 font-medium tabular-nums">Campaigns</th>
              <th className="w-1/2 py-2 pr-4 font-medium">Strong / Medium / Weak</th>
              <th className="py-2 font-medium">Top channel</th>
            </tr>
          </thead>
          <tbody>
            {perCompetitor.map((p) => (
              <tr key={p.name} className="border-t border-slate-100">
                <td className="py-2.5 pr-4 font-medium">{p.name}</td>
                <td className="py-2.5 pr-4 tabular-nums text-slate-600">{p.total}</td>
                <td className="py-2.5 pr-4">
                  <div className="flex items-center gap-2">
                    <StrengthBar name={p.name} counts={p.counts} max={maxTotal} />
                    <span className="whitespace-nowrap text-xs tabular-nums text-slate-500">
                      {p.counts.Strong} / {p.counts.Medium} / {p.counts.Weak}
                    </span>
                  </div>
                </td>
                <td className="py-2.5 text-slate-600">{p.channel}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {/* Recommendations + fact checks */}
      <div className="grid gap-5 md:grid-cols-3">
        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm md:col-span-2">
          <h3 className="mb-1 font-semibold">Recommendations for {data.my_company.name}</h3>
          {recs.length > 0 ? (
            <>
              <p className="mb-4 text-xs text-slate-500">
                Written by Exa. Each one must cite campaigns from this scan, linked below it.
              </p>
              <ol className="mb-5 space-y-4">
                {recs.map((r, i) => (
                  <li key={i} className="flex gap-3">
                    <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-slate-900 text-xs font-semibold text-white">
                      {i + 1}
                    </span>
                    <div className="text-sm">
                      <div className="font-semibold text-slate-900">{r.title}</div>
                      <div className="mt-0.5 text-slate-700">{r.action}</div>
                      <div className="mt-0.5 text-xs text-slate-500">{r.why}</div>
                      <div className="mt-1.5 flex flex-wrap gap-1.5">
                        {r.evidence.map((e) => {
                          const yours = e.competitor === data.my_company.name;
                          return (
                            <a
                              key={e.url}
                              href={e.url}
                              target="_blank"
                              rel="noreferrer"
                              title={e.title}
                              className={`max-w-[16rem] truncate rounded-full px-2 py-0.5 text-xs hover:bg-indigo-50 ${
                                yours ? "bg-white text-slate-600 ring-1 ring-slate-300" : "bg-slate-100 text-indigo-700"
                              }`}
                            >
                              {yours ? "Yours" : e.competitor}: {e.title}
                            </a>
                          );
                        })}
                      </div>
                      {r.flags?.map((f) => (
                        <div key={f} className="mt-1 text-xs font-medium text-amber-700">⚠ {f}</div>
                      ))}
                    </div>
                  </li>
                ))}
              </ol>
            </>
          ) : (
            <p className="mb-4 text-xs text-slate-500">
              {data.mock
                ? "Mock runs skip the written recommendations. Here are the highlights instead."
                : "No recommendations were generated for this run. Here are the highlights instead."}
            </p>
          )}
          <div className={recs.length ? "border-t border-slate-100 pt-4" : ""}>
            {recs.length > 0 && <h4 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Highlights</h4>}
            {data.strong.length > 0 && (
              <>
                <p className="mb-2 text-sm text-slate-600">Strongest competitor campaigns:</p>
                <ul className="mb-3 space-y-1.5 text-sm">
                  {data.strong.map((c, i) => (
                    <li key={i}>
                      <span className="font-medium">{c.competitor}:</span>{" "}
                      <a href={c.url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline">
                        {c.title}
                      </a>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {data.top_channels.length > 0 && (
              <p className="text-sm text-slate-600">
                Busiest channels:{" "}
                {data.top_channels.map((t) => (
                  <span key={t.channel} className="mr-1.5 inline-block rounded-full bg-slate-100 px-2 py-0.5 text-xs font-medium text-slate-700">
                    {t.channel} ({t.count})
                  </span>
                ))}
              </p>
            )}
          </div>
        </div>

        <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
          <h3 className="mb-3 font-semibold">Fact checks</h3>
          <dl className="space-y-2 text-sm">
            {[
              ["Different company or product", data.checks.excluded_off_topic, "excluded"],
              [`Older than ${data.checks.max_age_days} days`, data.checks.excluded_old, "excluded"],
              ["Numbers not found in source", data.checks.unverified_numbers, "confidence set to Low"],
              ["No known date", data.checks.undated, "kept, check manually"],
            ].map(([label, n, note]) => (
              <div key={label as string} className="flex items-baseline justify-between gap-3">
                <dt className="text-slate-600">
                  {label}
                  <div className="text-xs text-slate-400">{note}</div>
                </dt>
                <dd className="text-lg font-semibold tabular-nums">{n}</dd>
              </div>
            ))}
          </dl>
        </div>
      </div>

      {/* Campaigns */}
      <div className="rounded-2xl border border-slate-200 bg-white p-5 shadow-sm">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h3 className="mr-auto font-semibold">Campaigns ({rows.length})</h3>
          <input
            className="min-w-48 rounded-lg border border-slate-300 px-3 py-1.5 text-sm print:hidden"
            placeholder="Filter campaigns…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <select
            className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm print:hidden"
            value={competitor}
            onChange={(e) => setCompetitor(e.target.value)}
          >
            {["All", ...perCompetitor.map((p) => p.name)].map((n) => (
              <option key={n} value={n}>
                {n === "All" ? "All competitors" : n}
              </option>
            ))}
          </select>
          <select
            className="rounded-lg border border-slate-300 px-2 py-1.5 text-sm print:hidden"
            value={strength}
            onChange={(e) => setStrength(e.target.value)}
          >
            {["All", "Strong", "Medium", "Weak", "TBD"].map((s) => (
              <option key={s} value={s}>
                {s === "All" ? "All strengths" : s}
              </option>
            ))}
          </select>
        </div>
        <div className="overflow-x-auto rounded-xl ring-1 ring-slate-200">
          <table className="w-full min-w-[860px] text-left text-sm">
            <thead className="bg-slate-50 text-xs text-slate-500">
              <tr>
                {["Competitor", "Campaign", "Channel", "Signal", "Strength", "Confidence"].map((h) => (
                  <th key={h} className="px-4 py-2 font-medium">
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((c, i) => (
                <tr key={i} className="border-t border-slate-100 align-top hover:bg-slate-50">
                  <td className="px-4 py-2 font-medium">{c.competitor}</td>
                  <td className="max-w-xs px-4 py-2">
                    <a href={c.url} target="_blank" rel="noreferrer" className="font-medium text-indigo-600 hover:underline">
                      {c.title}
                    </a>
                    {c.date && <div className="text-xs text-slate-400">{c.date}</div>}
                    {c.message && <div className="mt-1 text-xs text-slate-500">{c.message}</div>}
                    {c.proof && (
                      <details className="mt-1 text-xs text-slate-500 print:hidden">
                        <summary className="cursor-pointer text-slate-400">Proof</summary>
                        <p className="mt-1 whitespace-pre-wrap">{c.proof.slice(0, 400)}</p>
                      </details>
                    )}
                  </td>
                  <td className="px-4 py-2 text-slate-600">{c.channel}</td>
                  <td className="max-w-sm px-4 py-2 text-slate-600">
                    {c.signal}
                    {c.why && <div className="mt-1 text-xs italic text-slate-400">{c.why}</div>}
                    {c.flags?.map((f) => (
                      <div key={f} className="mt-1 text-xs font-medium text-amber-700">
                        ⚠ {f}
                      </div>
                    ))}
                  </td>
                  <td className="px-4 py-2">
                    <StrengthTag value={c.strength} />
                  </td>
                  <td className="px-4 py-2 text-slate-600">{c.confidence}</td>
                </tr>
              ))}
              {rows.length === 0 && (
                <tr>
                  <td colSpan={6} className="px-4 py-6 text-center text-slate-400">
                    No campaigns match the current filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Own campaigns */}
      {(data.own_campaigns?.length ?? 0) > 0 && (
        <details className="rounded-2xl border border-slate-200 bg-white p-5 text-sm shadow-sm">
          <summary className="cursor-pointer font-semibold">
            {data.my_company.name}'s own recent campaigns ({data.own_campaigns!.length})
          </summary>
          <p className="mt-2 text-xs text-slate-500">
            Found with the same searches. The recommendations are told not to repeat these.
          </p>
          <ul className="mt-3 space-y-1.5">
            {data.own_campaigns!.map((c, i) => (
              <li key={i} className="text-slate-600">
                <a href={c.url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline">
                  {c.title}
                </a>
                {c.date && <span className="text-xs text-slate-400"> · {c.date}</span>}
                {c.message && <div className="text-xs text-slate-500">{c.message}</div>}
              </li>
            ))}
          </ul>
        </details>
      )}

      {/* Excluded + method */}
      {data.excluded.length > 0 && (
        <details className="rounded-2xl border border-slate-200 bg-white p-5 text-sm shadow-sm">
          <summary className="cursor-pointer font-semibold">Excluded from analysis ({data.excluded.length})</summary>
          <ul className="mt-3 space-y-1.5">
            {data.excluded.map((c, i) => (
              <li key={i} className="text-slate-600">
                <span className="font-medium">{c.competitor}:</span>{" "}
                <a href={c.url} target="_blank" rel="noreferrer" className="text-indigo-600 hover:underline">
                  {c.title}
                </a>{" "}
                <span className="text-xs text-slate-400">— {c.excluded}</span>
              </li>
            ))}
          </ul>
        </details>
      )}
      <details className="rounded-2xl border border-slate-200 bg-white p-5 text-sm shadow-sm">
        <summary className="cursor-pointer font-semibold">Method and limitations</summary>
        <ul className="mt-3 list-disc space-y-1 pl-5 text-slate-600">
          {data.method.map((m) => (
            <li key={m}>{m}</li>
          ))}
        </ul>
      </details>

      <p className="text-xs text-slate-500 print:hidden">
        Trace:{" "}
        {phoenix ? (
          <a
            href={`${PHOENIX_URL}/redirects/traces/${data.otel_trace_id}`}
            target="_blank"
            rel="noreferrer"
            className="text-indigo-600 hover:underline"
          >
            View this run in Phoenix
          </a>
        ) : (
          <a href={`/api/traces/${data.run_id}`} target="_blank" rel="noreferrer" className="font-mono hover:underline">
            traces/{data.run_id}.jsonl
          </a>
        )}
      </p>
    </section>
  );
}
