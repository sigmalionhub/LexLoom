/**
 * Shared visual primitives for panel sections.
 *
 * These components define the consistent look and layout of section headers and
 * data rows. Exported so later sections can match the panel's native style
 * without duplicating markup.
 */

export function Section({
  label,
  children,
  last = false,
}: {
  label: string;
  children: React.ReactNode;
  last?: boolean;
}) {
  return (
    <section className={last ? 'pt-5' : 'border-b border-slate-rule pb-5 pt-5'}>
      <h3 className="label-micro mb-2.5">{label}</h3>
      {children}
    </section>
  );
}

export function Row({
  label,
  children,
  wrap = false,
}: {
  label: string;
  children: React.ReactNode;
  wrap?: boolean;
}) {
  return (
    <div className="flex items-baseline justify-between gap-4 py-[3px]">
      <span className="font-mono text-data text-mist">{label}</span>
      <span
        className={`figure text-data text-chalk ${wrap ? 'break-all text-right' : 'whitespace-nowrap'}`}
      >
        {children}
      </span>
    </div>
  );
}
