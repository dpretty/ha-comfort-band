/**
 * Bundle entry point.
 *
 * Importing this file registers `<comfort-band-card>` (and its sub-elements
 * via transitive imports) and pushes the card into HA's `customCards`
 * registry so it appears in the dashboard's "Add card" picker.
 */

import './comfort-band-card.js';

declare global {
  interface Window {
    customCards?: Array<{
      type: string;
      name: string;
      description: string;
      preview?: boolean;
    }>;
  }
}

(window.customCards ??= []).push({
  type: 'comfort-band-card',
  name: 'Comfort Band',
  description: 'Schedule editor and live status for a Comfort Band zone.',
  preview: false,
});

console.info(
  '%c COMFORT-BAND-CARD %c v0.1.0 ',
  'color:white;background:#2196F3;padding:2px 4px;border-radius:3px',
  'color:#000;background:#fff;padding:2px 4px;border-radius:3px',
);
