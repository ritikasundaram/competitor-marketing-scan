// Field marketing focus: a research dashboard of rivals' events (no action plan).
import { useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { ReportData, RivalEvent } from "./api";
import { Card, Chip, SourceLinks, ThreatChip } from "./ui";

// One hue for every chart: these are counts and sizes, not categories.
const BAR = "#3987e5";
const BAR_DARK = "#184f95";
const BAR_LIGHT = "#c9ddf6";

const TODAY = new Date(); // "upcoming" means after the day the dashboard was opened

type Row = { rival: string; e: RivalEvent; when: Date | null };

export function eventRows(d: ReportData): Row[] {
  return d.profiles.flatMap((p) => (p.events ?? []).map((e) => ({ rival: p.rival.name, e, when: parseDate(e.date) })));
}

/** "2026-11-06", "2026-11" or "2026" → a date (mid-month / mid-year when only part is known). */
export function parseDate(s: string): Date | null {
  const m = /(\d{4})(?:-(\d{1,2}))?(?:-(\d{1,2}))?/.exec(s || "");
  if (!m) return null;
  const d = new Date(Number(m[1]), m[2] ? Number(m[2]) - 1 : 6, m[3] ? Number(m[3]) : m[2] ? 15 : 1);
  return isNaN(d.getTime()) ? null : d;
}

/** "about 124 registered" → 124; "50–200 attendees" → 50 to 200; "100+ builders" → 100. Years are ignored. */
export function parseCount(s: string): { lo: number; hi: number } | null {
  const nums: number[] = [];
  for (const m of (s || "").matchAll(/(\d[\d,]*(?:\.\d+)?)\s*([kK])?\b/g)) {
    const raw = m[1].replace(/,/g, "");
    const n = Number(raw) * (m[2] ? 1000 : 1);
    if (!m[2] && !m[1].includes(",") && /^(19|20)\d\d$/.test(raw)) continue; // a year, not a headcount
    if (n > 0) nums.push(n);
  }
  if (!nums.length) return null;
  const range = /\d\s*(?:-|–|—|to)\s*\d/.test(s);
  return range && nums.length >= 2 ? { lo: Math.min(nums[0], nums[1]), hi: Math.max(nums[0], nums[1]) } : { lo: nums[0], hi: nums[0] };
}

function city(e: RivalEvent): string {
  if (e.format === "virtual" || /online|virtual/i.test(e.location)) return "Online";
  const place = (e.location || "").split("(")[0].split(",")[0].trim();
  return place && !/unknown/i.test(place) ? place : "";
}

const fmtDate = (d: Date) => d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
const fmtNum = (n: number) => n.toLocaleString("en-US");

// ---------- summary ----------

export function KeyFindings({ d }: { d: ReportData }) {
  const s = d.summary;
  return (
    <Card className="print-keep border-blue-200 bg-blue-50/40">
      <p className="text-xs font-semibold uppercase tracking-wide text-blue-800">Key findings</p>
      {s?.headline && <p className="mt-1 text-lg font-medium text-slate-900">{s.headline}</p>}
      {s?.findings?.length ? (
        <ul className="mt-3 space-y-2 text-sm">
          {s.findings.map((f, i) => (
            <li key={i} className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
              <span className="text-slate-800"><span aria-hidden className="mr-2 text-blue-700">•</span>{f.text}</span>
              <span className="flex shrink-0 flex-wrap items-center gap-1 sm:ml-auto">
                <SourceLinks ids={f.source_ids} sources={d.sources} />
                {f.flags.map((fl) => <Chip key={fl} tone="amber" title={fl}>⚠ number not in sources</Chip>)}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        !s && <p className="mt-2 text-xs text-slate-500">This report was made before key findings were added. Run a new field scan to get them.</p>
      )}
    </Card>
  );
}

export function FieldTiles({ d }: { d: ReportData }) {
  const rows = eventRows(d);
  const st = d.stats;
  const now = TODAY;
  const active = new Set(rows.map((r) => r.rival)).size;
  const upcoming = rows.filter((r) => r.when && r.when >= now).length;
  const removed = (st.event_metrics_flagged ?? 0) + (st.events_dropped ?? 0);
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
      <Tile label="Rival events found" value={rows.length} note={`across ${active} of ${d.rivals.length} rivals`} />
      <Tile label="Upcoming" value={upcoming} note="dated after today" />
      <Tile label="With attendance" value={st.events_with_attendance ?? 0} note="each figure from a source" />
      <Tile label="With social engagement" value={st.events_with_engagement ?? 0} note="LinkedIn, X, YouTube, Instagram" />
      <Tile label="Removed by checks" value={removed} note={`${st.events_dropped ?? 0} events, ${st.event_metrics_flagged ?? 0} figures`} />
    </div>
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

// ---------- charts ----------

/** Hover or focus to see the details. The tooltip is drawn on the page body, so scroll boxes can't clip it. */
function Tip({ children, tip, className = "" }: { children: ReactNode; tip: ReactNode; className?: string }) {
  const [at, setAt] = useState<{ x: number; y: number; below: boolean } | null>(null);
  const show = (el: HTMLElement) => {
    const r = el.getBoundingClientRect();
    const below = r.top < 110; // too close to the top of the window: show it underneath
    setAt({ x: Math.min(Math.max(r.left + r.width / 2, 140), window.innerWidth - 140), y: below ? r.bottom : r.top, below });
  };
  return (
    <span tabIndex={0} className={`outline-none focus-visible:ring-2 focus-visible:ring-blue-500 rounded-full ${className}`}
      onMouseEnter={(e) => show(e.currentTarget)} onMouseLeave={() => setAt(null)}
      onFocus={(e) => show(e.currentTarget)} onBlur={() => setAt(null)}>
      {children}
      {at && createPortal(
        <span role="tooltip" style={{ position: "fixed", left: at.x, top: at.y,
          transform: at.below ? "translate(-50%, 8px)" : "translate(-50%, calc(-100% - 8px))" }}
          className="pointer-events-none z-50 block w-max max-w-64 rounded-lg bg-slate-900 px-3 py-2 text-left text-xs leading-snug text-white shadow-lg">
          {tip}
        </span>, document.body)}
    </span>
  );
}

/** Dots closer than `gap` (a fraction of the track) go on the next lane down, so none hide another. */
function lanes(xs: number[], gap = 0.03): number[] {
  const last: number[] = [];
  return xs.map((x) => {
    let lane = last.findIndex((l) => x - l >= gap);
    if (lane < 0) lane = last.length;
    last[lane] = x;
    return lane;
  });
}

export function Timeline({ d }: { d: ReportData }) {
  const rows = eventRows(d);
  const now = TODAY;
  const dated = rows.filter((r) => r.when) as (Row & { when: Date })[];
  const year = 365 * 864e5;
  const start = new Date(Math.min(now.getTime() - year, ...dated.map((r) => r.when.getTime())));
  start.setDate(1);
  const end = new Date(Math.max(now.getTime() + year / 4, ...dated.map((r) => r.when.getTime())));
  end.setMonth(end.getMonth() + 1, 1);
  const span = end.getTime() - start.getTime();
  const x = (t: Date) => `${((t.getTime() - start.getTime()) / span) * 100}%`;
  const months: Date[] = [];
  for (const m = new Date(start); m <= end; m.setMonth(m.getMonth() + 1)) months.push(new Date(m));
  const every = Math.ceil(months.length / 8);
  const rivals = d.profiles.map((p) => p.rival.name);
  return (
    <Card className="print-keep">
      <ChartHead title="Event calendar" note="Each dot is one event. Filled dots already happened; open dots are upcoming." />
      <div className="overflow-x-auto">
        <div className="min-w-[560px]">
          {rivals.map((name) => {
            const mine = dated.filter((r) => r.rival === name).sort((a, b) => a.when.getTime() - b.when.getTime());
            const lane = lanes(mine.map((r) => (r.when.getTime() - start.getTime()) / span));
            const height = 24 + Math.max(0, ...lane) * 18;
            const undated = rows.filter((r) => r.rival === name && !r.when).length;
            return (
              <div key={name} className="grid grid-cols-[96px_1fr_64px] items-start gap-2 border-b border-slate-100 py-2.5">
                <span className="truncate pt-0.5 text-sm font-medium" title={name}>{name}</span>
                <div className="relative" style={{ height }}>
                  <div className="absolute inset-x-0 top-3 h-px bg-slate-200" />
                  <div className="absolute -inset-y-2.5 border-l border-dashed border-slate-400" style={{ left: x(now) }} />
                  {mine.map((r, i) => (
                    <span key={i} className="absolute" style={{ left: x(r.when), top: 12 + lane[i] * 18 }}>
                      <Tip className="block -translate-x-1/2 -translate-y-1/2" tip={
                        <><b className="block">{r.e.name}</b>{fmtDate(r.when)} · {r.e.kind}{city(r.e) ? ` · ${city(r.e)}` : ""}
                          {r.e.attendees && <span className="block text-slate-300">{r.e.attendees}</span>}</>}>
                        <span className="block h-3.5 w-3.5 rounded-full ring-2 ring-white" aria-label={`${r.e.name}, ${fmtDate(r.when)}`}
                          style={r.when < now ? { background: BAR_DARK } : { background: "#fff", boxShadow: `inset 0 0 0 2.5px ${BAR}` }} />
                      </Tip>
                    </span>
                  ))}
                </div>
                <span className="pt-1 text-right text-xs text-slate-500">{undated ? `+${undated} undated` : ""}</span>
              </div>
            );
          })}
          <div className="grid grid-cols-[96px_1fr_64px] gap-2 pt-1">
            <span />
            <div className="relative h-8">
              {months.map((m, i) => i % every === 0 && (
                <span key={i} className="absolute -translate-x-1/2 text-[11px] text-slate-500" style={{ left: x(m) }}>
                  {m.toLocaleDateString("en-GB", { month: "short" })}{m.getMonth() === 0 || i === 0 ? ` ’${String(m.getFullYear()).slice(2)}` : ""}
                </span>
              ))}
              <span className="absolute top-4 -translate-x-1/2 text-[11px] font-semibold text-slate-700" style={{ left: x(now) }}>Today</span>
            </div>
            <span />
          </div>
        </div>
      </div>
    </Card>
  );
}

export function PerRival({ d }: { d: ReportData }) {
  const rows = eventRows(d);
  const data = d.profiles.map((p) => {
    const mine = rows.filter((r) => r.rival === p.rival.name);
    const kinds = count(mine.map((r) => r.e.kind));
    return { name: p.rival.name, n: mine.length, kinds };
  }).sort((a, b) => b.n - a.n);
  const max = Math.max(1, ...data.map((r) => r.n));
  return (
    <Card className="print-keep">
      <ChartHead title="Events per rival" note="Hosted, co-hosted or sponsored, after the checks." />
      <ul className="space-y-2.5">
        {data.map((r) => (
          <li key={r.name} className="grid grid-cols-[96px_1fr] items-center gap-2 text-sm">
            <span className="truncate font-medium" title={r.name}>{r.name}</span>
            <Tip className="block" tip={r.kinds.length ? r.kinds.map(([k, n]) => `${n} ${k}`).join(" · ") : "No events found"}>
              <span className="flex items-center gap-2">
                <span className="h-4 rounded-r" style={{ width: `${(r.n / max) * 85}%`, minWidth: r.n ? 4 : 0, background: BAR }} />
                <span className="tabular-nums text-slate-700">{r.n}</span>
              </span>
            </Tip>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function Attendance({ d }: { d: ReportData }) {
  const rows = eventRows(d)
    .map((r) => ({ ...r, c: parseCount(r.e.attendees) }))
    .filter((r): r is Row & { c: { lo: number; hi: number } } => !!r.c)
    .sort((a, b) => b.c.hi - a.c.hi);
  const max = Math.max(1, ...rows.map((r) => r.c.hi));
  return (
    <Card className="print-keep">
      <ChartHead title="Attendance, where a source states it" note="Ranges show as a lighter band. Hover for the exact wording." />
      {!rows.length && <p className="text-sm text-slate-500">No event had an attendance figure a source shows.</p>}
      <ul className="space-y-3">
        {rows.map((r, i) => (
          <li key={i} className="text-sm">
            <p className="truncate"><span className="font-medium">{r.e.name}</span> <span className="text-xs text-slate-500">· {r.rival}</span></p>
            <Tip className="mt-1 block" tip={<>“{r.e.attendees}”</>}>
              <span className="flex items-center gap-2">
                <span className="flex h-3.5" style={{ width: `${(r.c.hi / max) * 80}%`, minWidth: 4 }}>
                  <span className="h-full rounded-l" style={{ width: `${(r.c.lo / r.c.hi) * 100}%`, background: BAR }} />
                  {r.c.hi > r.c.lo && <span className="h-full flex-1 rounded-r" style={{ background: BAR_LIGHT }} />}
                </span>
                <span className="shrink-0 tabular-nums text-slate-700">
                  {r.c.hi > r.c.lo ? `${fmtNum(r.c.lo)}–${fmtNum(r.c.hi)}` : fmtNum(r.c.hi)}{/\+/.test(r.e.attendees) ? "+" : ""}
                </span>
              </span>
            </Tip>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export function Mix({ d }: { d: ReportData }) {
  const rows = eventRows(d);
  const lists: [string, [string, number][]][] = [
    ["Event types", count(rows.map((r) => r.e.kind))],
    ["Formats", count(rows.map((r) => r.e.format).filter((f) => f !== "unknown"))],
    ["Top places", count(rows.map((r) => city(r.e)).filter(Boolean)).slice(0, 6)],
  ];
  return (
    <Card className="print-keep">
      <ChartHead title="What kind of events" note="Counts across all rivals." />
      <div className="grid gap-5 sm:grid-cols-3">
        {lists.map(([title, items]) => {
          const max = Math.max(1, ...items.map(([, n]) => n));
          return (
            <div key={title} className="min-w-0">
              <h5 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h5>
              <ul className="space-y-1.5 text-sm">
                {items.map(([k, n]) => (
                  <li key={k} className="grid grid-cols-[1fr_auto] items-center gap-x-2">
                    <span className="truncate capitalize" title={k}>{k}</span>
                    <span className="tabular-nums text-slate-600">{n}</span>
                    <span className="col-span-2 h-1.5 rounded-full bg-slate-100">
                      <span className="block h-full rounded-full" style={{ width: `${(n / max) * 100}%`, background: BAR }} />
                    </span>
                  </li>
                ))}
                {!items.length && <li className="text-slate-500">Not known</li>}
              </ul>
            </div>
          );
        })}
      </div>
    </Card>
  );
}

function ChartHead({ title, note }: { title: string; note: string }) {
  return (
    <div className="mb-4">
      <h4 className="text-sm font-semibold">{title}</h4>
      <p className="text-xs text-slate-500">{note}</p>
    </div>
  );
}

function count(xs: string[]): [string, number][] {
  const m = new Map<string, number>();
  for (const x of xs) m.set(x, (m.get(x) ?? 0) + 1);
  return [...m.entries()].sort((a, b) => b[1] - a[1]);
}

// ---------- rivals ----------

export function RivalEventProfiles({ d }: { d: ReportData }) {
  const order = { High: 0, Medium: 1, Low: 2 };
  const now = TODAY;
  const profiles = [...d.profiles].sort((a, b) => order[a.threat] - order[b.threat]);
  return (
    <section>
      <h3 className="mb-3 text-lg font-semibold">Rival event profiles</h3>
      <div className="grid gap-4 md:grid-cols-2">
        {profiles.map((p) => {
          const evs = (p.events ?? []).map((e) => ({ e, when: parseDate(e.date) }));
          const next = evs.filter((x) => x.when && x.when >= now).sort((a, b) => a.when!.getTime() - b.when!.getTime())[0];
          const last = evs.filter((x) => x.when && x.when < now).sort((a, b) => b.when!.getTime() - a.when!.getTime())[0];
          const biggest = evs.map((x) => ({ ...x, c: parseCount(x.e.attendees) })).filter((x) => x.c)
            .sort((a, b) => b.c!.hi - a.c!.hi)[0];
          return (
            <Card key={p.rival.name} className="print-keep">
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div>
                  <h4 className="font-semibold">{p.rival.name}</h4>
                  <p className="text-xs text-slate-500">{p.rival.domain} · {evs.length} event{evs.length === 1 ? "" : "s"}</p>
                </div>
                <ThreatChip level={p.threat} />
              </div>
              <p className="mt-3 text-sm text-slate-800">{p.summary}</p>
              <p className="mt-2 text-sm text-slate-600"><span className="font-medium text-slate-700">How they use events: </span>{p.positioning}</p>
              <dl className="mt-4 grid gap-3 border-t border-slate-100 pt-3 text-sm sm:grid-cols-3">
                <Fact label="Most recent" e={last?.e} when={last?.when} />
                <Fact label="Next up" e={next?.e} when={next?.when} />
                <div className="min-w-0">
                  <dt className="text-xs text-slate-500">Biggest stated turnout</dt>
                  {biggest ? <dd><span className="font-medium">{biggest.e.attendees}</span><span className="block truncate text-xs text-slate-500" title={biggest.e.name}>{biggest.e.name}</span></dd>
                    : <dd className="text-slate-400">not found</dd>}
                </div>
              </dl>
              {!evs.length && <p className="mt-3 text-sm text-slate-500">No events passed the checks.</p>}
            </Card>
          );
        })}
      </div>
    </section>
  );
}

function Fact({ label, e, when }: { label: string; e?: RivalEvent; when?: Date | null }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-slate-500">{label}</dt>
      {e && when ? (
        <dd><span className="block truncate font-medium" title={e.name}>{e.name}</span><span className="text-xs text-slate-500">{fmtDate(when)}{city(e) ? ` · ${city(e)}` : ""}</span></dd>
      ) : <dd className="text-slate-400">none</dd>}
    </div>
  );
}
