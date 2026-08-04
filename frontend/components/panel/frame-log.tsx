import type { Frame } from '@/lib/stream';
import type { Turn } from '@/lib/use-lexloom';

interface FrameGroup {
  name: Frame['name'];
  at: number;
  until?: number;
  count: number;
}

/**
 * Forty-one consecutive `token` rows would bury the four frames whose order is
 * the point, so runs of tokens collapse into one row that keeps the count and
 * the span. Every other frame is listed individually, in the order it arrived.
 */
function groupFrames(frames: Frame[]): FrameGroup[] {
  const groups: FrameGroup[] = [];
  for (const frame of frames) {
    const last = groups[groups.length - 1];
    if (frame.name === 'token' && last?.name === 'token') {
      last.count += 1;
      last.until = frame.at;
      continue;
    }
    groups.push({ name: frame.name, at: frame.at, count: 1 });
  }
  return groups;
}

export function FrameLog({ turn }: { turn: Turn | null }) {
  if (!turn || turn.frames.length === 0) {
    return <p className="font-mono text-data text-mist">No stream open.</p>;
  }

  const groups = groupFrames(turn.frames);
  return (
    <ol className="space-y-[3px]">
      {groups.map((group, index) => (
        <li
          key={`${group.name}-${index}`}
          className="flex animate-rise-in items-baseline gap-3 font-mono text-data"
        >
          <span
            aria-hidden
            className={`mt-[1px] size-[6px] shrink-0 rounded-full ${
              group.name === 'citations' ? 'bg-brass' : group.name === 'error' ? 'bg-alarm' : 'bg-chalk/55'
            }`}
          />
          <span className="min-w-0 flex-1 truncate text-chalk">
            {group.name}
            {group.count > 1 && <span className="text-mist"> ×{group.count}</span>}
          </span>
          <span className="figure shrink-0 text-mist">
            {group.until !== undefined ? `${group.at}–${group.until}` : group.at} ms
          </span>
        </li>
      ))}
      {turn.status === 'complete' && (
        <li className="flex items-baseline gap-3 pt-1 font-mono text-data text-mist">
          <span className="w-[6px] shrink-0" />
          <span className="flex-1">
            finish_reason <span className="text-chalk">{turn.finishReason}</span>
          </span>
          <span className="figure">{turn.tokens} tokens</span>
        </li>
      )}
    </ol>
  );
}
