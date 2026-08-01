'use client';

import type { Turn } from '@/lib/use-lexloom';

/**
 * The claim this whole service is shaped around is that retrieval finishes
 * before generation begins, so the sources can render while the answer is
 * still typing. This draws that claim to scale.
 *
 * Both axes are linear and both are measured. The problem with a single linear
 * axis is that it tells the truth too well: against a stream that runs for a
 * second and a bit, the interval the reader came to see is four pixels wide.
 * So there are two — the full stream, and the opening of it magnified, with
 * the magnification factor computed and printed rather than implied. An axis
 * that quietly changed scale to flatter the numbers would be worth nothing
 * here.
 */

interface StreamTimelineProps {
  turn: Turn | null;
}

interface Mark {
  at: number;
  kind: 'plain' | 'measured';
  title: string;
}

export function StreamTimeline({ turn }: StreamTimelineProps) {
  const t = turn?.timings;
  const started = Boolean(turn && turn.frames.length > 0);

  // Nothing has been measured, so nothing is drawn. An axis with no data on it
  // would just be furniture.
  if (!turn || !t || !started) return null;

  const total = Math.max(t.end ?? t.lastToken ?? t.firstToken ?? t.citations ?? 0, 1);
  const marks: Mark[] = [{ at: 0, kind: 'plain', title: 'message_start' }];
  if (t.citations !== undefined) marks.push({ at: t.citations, kind: 'measured', title: 'citations' });
  if (t.firstToken !== undefined) marks.push({ at: t.firstToken, kind: 'plain', title: 'first token' });
  if (t.end !== undefined) marks.push({ at: t.end, kind: 'plain', title: 'message_end' });

  const band =
    t.firstToken !== undefined && t.lastToken !== undefined
      ? { from: t.firstToken, to: t.lastToken }
      : null;

  // The magnified axis frames the interval that matters: from zero to a little
  // past the first token.
  const zoomAnchor = t.firstToken ?? t.citations;
  const zoomRange = zoomAnchor === undefined ? null : niceCeiling(zoomAnchor * 1.6);
  const magnification = zoomRange ? total / zoomRange : 1;
  const showZoom = zoomRange !== null && magnification >= 1.5;

  const gap = t.citations !== undefined && t.firstToken !== undefined ? t.firstToken - t.citations : null;

  return (
    <div className="space-y-5">
      <Axis caption="full stream" range={total} marks={marks} band={band} />

      {showZoom && zoomRange !== null && (
        <Axis
          caption={`first ${zoomRange} ms · ${magnification.toFixed(0)}× magnified`}
          range={zoomRange}
          marks={marks.filter((mark) => mark.at <= zoomRange)}
          band={
            band && band.from <= zoomRange ? { from: band.from, to: Math.min(band.to, zoomRange) } : null
          }
          bracket={
            gap !== null && t.citations !== undefined && t.firstToken !== undefined
              ? { from: t.citations, to: t.firstToken, label: `${gap} ms` }
              : null
          }
        />
      )}

      <p className="flex flex-wrap items-center gap-x-4 gap-y-1 font-mono text-[0.6875rem] text-mist">
        <Key swatch="measured">citations</Key>
        <Key swatch="band">token frames</Key>
      </p>
    </div>
  );
}

interface AxisProps {
  caption: string;
  range: number;
  marks: Mark[];
  band: { from: number; to: number } | null;
  bracket?: { from: number; to: number; label: string } | null;
}

function Axis({ caption, range, marks, band, bracket = null }: AxisProps) {
  const pct = (value: number) => `${Math.min(100, Math.max(0, (value / range) * 100))}%`;

  return (
    <figure>
      <figcaption className="mb-2 flex items-baseline justify-between gap-3">
        <span className="label-micro">{caption}</span>
        <span className="figure text-data text-mist">0 – {Math.round(range).toLocaleString('en')} ms</span>
      </figcaption>

      {/* The bracket needs a lane of its own above the track, or its figure
          lands on the caption. */}
      <div className={`relative ${bracket ? 'h-12' : 'h-6'}`}>
        {bracket && (
          <div
            className="absolute bottom-[18px] h-[10px] border-x border-t border-brass/70 transition-[left,width] duration-200 ease-out"
            style={{
              left: pct(bracket.from),
              width: `max(1px, ${pct(bracket.to - bracket.from)})`,
            }}
          >
            <span className="figure absolute -top-1 left-1/2 -translate-x-1/2 -translate-y-full whitespace-nowrap text-data font-medium text-brass">
              {bracket.label}
            </span>
          </div>
        )}

        <div className="absolute inset-x-0 bottom-2 h-px bg-slate-rule">
          {band && (
            <div
              className="absolute -top-[3px] h-[7px] rounded-sm bg-brass-dim/60 transition-[left,width] duration-200 ease-out"
              style={{
                left: pct(band.from),
                width: `max(2px, ${pct(band.to - band.from)})`,
              }}
            />
          )}

          {marks.map((mark) => (
            <div
              key={mark.title}
              title={`${mark.title} · ${mark.at} ms`}
              className="absolute -top-[6px] transition-[left] duration-200 ease-out"
              style={{ left: pct(mark.at) }}
            >
              <span
                className={
                  mark.kind === 'measured'
                    ? 'block h-[13px] w-[3px] -translate-x-1/2 rounded-[1px] bg-brass'
                    : 'block h-[13px] w-px -translate-x-1/2 bg-chalk/70'
                }
              />
            </div>
          ))}
        </div>
      </div>
    </figure>
  );
}

function Key({ swatch, children }: { swatch: 'measured' | 'band'; children: React.ReactNode }) {
  const swatchClass =
    swatch === 'measured'
      ? 'h-[9px] w-[3px] rounded-[1px] bg-brass'
      : 'h-[5px] w-3 rounded-sm bg-brass-dim/60';
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className={swatchClass} aria-hidden />
      {children}
    </span>
  );
}

/** Rounds up to the next 1, 2 or 5 × 10ⁿ so the axis ends on a number worth reading. */
function niceCeiling(value: number): number {
  if (value <= 0) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(value));
  const normalised = value / magnitude;
  const step = normalised <= 1 ? 1 : normalised <= 2 ? 2 : normalised <= 5 ? 5 : 10;
  return step * magnitude;
}
