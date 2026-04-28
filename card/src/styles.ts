/**
 * Shared CSS tokens for the Comfort Band card.
 *
 * Action colours follow the convention used elsewhere in HA: red for
 * heating, blue for cooling, neutral grey for idle, light grey for unknown.
 * Falls back to HA theme variables so themes can override.
 */

import { css } from 'lit';
import type { ComfortBandAction } from './types.js';

export const tokens = css`
  :host {
    --cb-action-heating: var(--cb-color-heat, var(--state-climate-heat-color, #d9603f));
    --cb-action-cooling: var(--cb-color-cool, var(--state-climate-cool-color, #2f7fcc));
    --cb-action-idle: var(--cb-color-idle, var(--state-inactive-color, #888888));
    --cb-action-unknown: var(--cb-color-unknown, var(--disabled-color, #bdbdbd));

    --cb-track-bg: var(--divider-color, #e0e0e0);
    --cb-text-primary: var(--primary-text-color, #212121);
    --cb-text-secondary: var(--secondary-text-color, #727272);

    --cb-radius-card: 12px;
    --cb-radius-pill: 999px;
    --cb-gap-xs: 4px;
    --cb-gap-sm: 8px;
    --cb-gap-md: 12px;
    --cb-gap-lg: 16px;
  }
`;

/** Maps an action string to its CSS custom-property reference. */
export function actionColorVar(action: ComfortBandAction | string | null | undefined): string {
  switch (action) {
    case 'heating':
      return 'var(--cb-action-heating)';
    case 'cooling':
      return 'var(--cb-action-cooling)';
    case 'idle':
      return 'var(--cb-action-idle)';
    default:
      return 'var(--cb-action-unknown)';
  }
}

/** Coerce any string to a known action, falling back to 'unknown'. */
export function asAction(value: unknown): ComfortBandAction {
  if (value === 'heating' || value === 'cooling' || value === 'idle') return value;
  return 'unknown';
}

/** Display label for an action (used in chips and a11y). */
export function actionLabel(action: ComfortBandAction): string {
  return action.charAt(0).toUpperCase() + action.slice(1);
}
