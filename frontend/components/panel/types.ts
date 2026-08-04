import type { RequestRecord } from '@/lib/api';
import type { IsolationReport, Tenant, Turn } from '@/lib/use-lexloom';

/**
 * Props passed to every panel section.
 *
 * Sections ignore fields they don't need. This contract is set once and does
 * not change, so five later authors never have to modify backend-panel.tsx
 * to add their props.
 */
export interface PanelSectionProps {
  tenant: Tenant | null;
  turn: Turn | null;
  requests: RequestRecord[];
  isolation: IsolationReport;
  tenantCount: number;
}
