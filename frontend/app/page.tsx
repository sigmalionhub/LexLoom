'use client';

import { BackendPanel } from '@/components/backend-panel';
import { Chat } from '@/components/chat';
import { useLexloom } from '@/lib/use-lexloom';

/**
 * Two halves, and only one of them is the product.
 *
 * On a wide screen the page is a fixed-height workbench: the transcript on the
 * left with its own scroll, the instrument on the right with its own. Below
 * 1024 px the panel stops being a column and becomes the section under the
 * chat — it collapses, it does not disappear. It is the argument.
 */
export default function Page() {
  const {
    tenants,
    activeTenant,
    activeTurns,
    currentTurn,
    elsewhereHasHistory,
    requests,
    isolation,
    busy,
    creatingTenant,
    fatal,
    ask,
    cancel,
    addTenant,
    switchTenant,
  } = useLexloom();

  return (
    <main className="grid min-h-screen grid-cols-1 lg:h-screen lg:grid-cols-[minmax(0,1fr)_25rem] xl:grid-cols-[minmax(0,1fr)_27rem]">
      <div className="min-h-0 lg:overflow-hidden">
        <Chat
          tenants={tenants}
          activeTenant={activeTenant}
          turns={activeTurns}
          busy={busy}
          creatingTenant={creatingTenant}
          fatal={fatal}
          elsewhereHasHistory={elsewhereHasHistory}
          onAsk={ask}
          onCancel={cancel}
          onAddTenant={() => void addTenant()}
          onSwitchTenant={switchTenant}
        />
      </div>

      <div className="min-h-0 border-t border-slate-rule lg:border-l lg:border-t-0">
        <BackendPanel
          tenant={activeTenant}
          turn={currentTurn}
          requests={requests}
          isolation={isolation}
          tenantCount={tenants.length}
        />
      </div>
    </main>
  );
}
