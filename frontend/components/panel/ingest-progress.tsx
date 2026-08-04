import type { PanelSectionProps } from './types';

/**
 * Ingestion progress section.
 *
 * Mounted but empty until M2.2 fills it in. The emptiness is the point: a section
 * that does not exist yet cannot mount itself, so every later phase would have to
 * edit backend-panel.tsx to add its import and element — which is the collision this
 * whole task exists to remove. Rendering null costs one component instance and buys
 * four branches that never touch a shared file.
 */
export function IngestProgress(_: PanelSectionProps) {
  return null;
}
