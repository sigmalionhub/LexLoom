import type { RequestRecord } from '@/lib/api';

/** Long uuids are shortened for width; the panel still shows the full org_id above. */
function shortPath(path: string): string {
  return path
    .replace('/api/v1', '')
    .replace(/\/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/g, '/…');
}

export function RequestLog({ requests }: { requests: RequestRecord[] }) {
  if (requests.length === 0) {
    return <p className="font-mono text-data text-mist">Nothing sent yet.</p>;
  }
  const recent = requests.slice(-7).reverse();
  return (
    <ol className="space-y-2">
      {recent.map((record) => (
        <li key={record.id} className="font-mono text-data">
          <div className="flex items-baseline gap-2">
            <span
              className={`figure shrink-0 ${
                record.status >= 200 && record.status < 300 ? 'text-chalk' : 'text-alarm'
              }`}
            >
              {record.status === 0 ? 'ERR' : record.status}
            </span>
            <span className="min-w-0 flex-1 truncate text-mist">
              <span className="text-chalk/80">{record.method}</span> {shortPath(record.path)}
            </span>
            <span className="figure shrink-0 text-mist">{record.ms} ms</span>
          </div>
          {record.requestId && (
            <div className="figure pl-8 text-[0.6875rem] text-mist/70">
              x-request-id {record.requestId.slice(0, 8)}…
              {record.code && <span className="text-alarm/80"> · {record.code}</span>}
            </div>
          )}
        </li>
      ))}
    </ol>
  );
}
