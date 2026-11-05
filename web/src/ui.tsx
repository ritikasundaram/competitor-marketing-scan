import type { ReactNode } from "react";
import type { Source } from "./api";

// Strength is a magnitude: one blue ramp, darker = stronger (text label always shown).
const STRENGTH: Record<string, { bg: string; fg: string }> = {
  Strong: { bg: "#184f95", fg: "#ffffff" },
  Medium: { bg: "#3987e5", fg: "#ffffff" },
  Weak: { bg: "#c9ddf6", fg: "#0f2f59" },
};

export function Chip({ children, tone = "slate", title }: { children: ReactNode; tone?: Tone; title?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${TONES[tone]}`}>
      {children}
    </span>
  );
}

type Tone = "slate" | "green" | "amber" | "red" | "blue" | "violet";
const TONES: Record<Tone, string> = {
  slate: "bg-slate-100 text-slate-700",
  green: "bg-emerald-50 text-emerald-800 ring-1 ring-emerald-200",
  amber: "bg-amber-50 text-amber-900 ring-1 ring-amber-200",
  red: "bg-rose-50 text-rose-800 ring-1 ring-rose-200",
  blue: "bg-blue-50 text-blue-800 ring-1 ring-blue-200",
  violet: "bg-violet-50 text-violet-800 ring-1 ring-violet-200",
};

export function StrengthPill({ value, note }: { value: string; note?: string }) {
  const c = STRENGTH[value] ?? STRENGTH.Weak;
  return (
    <span
      title={note || `${value} by the rubric`}
      className="inline-flex items-center rounded-full px-2 py-0.5 text-xs font-semibold"
      style={{ background: c.bg, color: c.fg }}
    >
      {value}
      {note ? " ↓" : ""}
    </span>
  );
}

const STATUS: Record<string, { tone: Tone; icon: string; label: string }> = {
  queued: { tone: "slate", icon: "○", label: "Queued" },
  researching: { tone: "blue", icon: "◔", label: "Researching" },
  retrying: { tone: "violet", icon: "↻", label: "Retrying" },
  answered: { tone: "green", icon: "✓", label: "Answered" },
  partial: { tone: "amber", icon: "◑", label: "Partly answered" },
  unanswered: { tone: "red", icon: "✕", label: "Not answered" },
};

export function StatusChip({ status }: { status: string }) {
  const s = STATUS[status] ?? STATUS.queued;
  return (
    <Chip tone={s.tone}>
      <span aria-hidden>{s.icon}</span>
      {s.label}
    </Chip>
  );
}

export function ThreatChip({ level }: { level: string }) {
  const tone: Tone = level === "High" ? "red" : level === "Medium" ? "amber" : "slate";
  return <Chip tone={tone}>{level} threat</Chip>;
}

export function SourceLinks({ ids, sources }: { ids: string[]; sources: Record<string, Source> }) {
  return (
    <span className="inline-flex flex-wrap gap-1">
      {ids.map((id) =>
        sources[id] ? (
          <a
            key={id}
            href={sources[id].url}
            target="_blank"
            rel="noreferrer"
            title={`${sources[id].title}${sources[id].date ? ` · ${sources[id].date}` : ""}\n${sources[id].url}`}
            className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700 hover:bg-blue-100 hover:text-blue-800"
          >
            {id} · {host(sources[id].url)}
          </a>
        ) : (
          <span key={id} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-400">
            {id}
          </span>
        ),
      )}
    </span>
  );
}

function host(url: string) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

export function Card({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <section className={`rounded-xl border border-slate-200 bg-white p-5 ${className}`}>{children}</section>;
}
