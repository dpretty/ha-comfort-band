/**
 * `<band-gauge>` — horizontal `[low … room … high]` gauge for a Comfort Band zone.
 *
 * Renders a fixed-range track (15–28 °C) with a coloured band stripe between
 * `low` and `high`, and a circular marker at `room`. The band's colour
 * reflects `current_action` (red=heating, blue=cooling, grey=idle/unknown).
 */

import { LitElement, html, css, svg } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { tokens, actionColorVar, asAction } from './styles.js';

const RANGE_MIN = 15;
const RANGE_MAX = 28;
const RANGE = RANGE_MAX - RANGE_MIN;

function pct(temp: number): number {
  if (Number.isNaN(temp) || !Number.isFinite(temp)) return 0;
  const clamped = Math.max(RANGE_MIN, Math.min(RANGE_MAX, temp));
  return ((clamped - RANGE_MIN) / RANGE) * 100;
}

@customElement('band-gauge')
export class BandGauge extends LitElement {
  @property({ type: Number }) public low = NaN;
  @property({ type: Number }) public high = NaN;
  @property({ type: Number }) public room = NaN;
  @property({ type: String }) public action: string | null = 'unknown';

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        width: 100%;
      }
      svg {
        display: block;
        width: 100%;
        height: 24px;
        overflow: visible;
      }
      .track {
        fill: var(--cb-track-bg);
      }
      .band {
        opacity: 0.85;
      }
      .marker-ring {
        fill: var(--ha-card-background, var(--card-background-color, #ffffff));
        stroke-width: 2;
      }
      .label {
        font-size: 11px;
        fill: var(--cb-text-secondary);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
      }
    `,
  ];

  protected override render() {
    const action = asAction(this.action);
    const color = actionColorVar(action);

    const lowKnown = Number.isFinite(this.low);
    const highKnown = Number.isFinite(this.high);
    const roomKnown = Number.isFinite(this.room);

    const lowPct = lowKnown ? pct(this.low) : 0;
    const highPct = highKnown ? pct(this.high) : 100;
    const bandX = Math.min(lowPct, highPct);
    const bandWidth = Math.max(0, Math.abs(highPct - lowPct));
    const roomPct = roomKnown ? pct(this.room) : 50;

    const fmt = (t: number) => (Number.isFinite(t) ? `${t.toFixed(1)}°` : '—');

    const aria = `Comfort band gauge: low ${fmt(this.low)}, room ${fmt(this.room)}, high ${fmt(this.high)}, action ${action}`;

    return html`
      <svg viewBox="0 0 100 24" preserveAspectRatio="none" role="img" aria-label=${aria}>
        ${svg`<rect class="track" x="0" y="10" width="100" height="4" rx="2"></rect>`}
        ${lowKnown && highKnown
          ? svg`<rect class="band" x=${bandX} y="9" width=${bandWidth} height="6" rx="3" fill=${color}></rect>`
          : null}
        ${roomKnown ? svg`<circle cx=${roomPct} cy="12" r="4.5" fill=${color}></circle>` : null}
        ${roomKnown
          ? svg`<circle class="marker-ring" cx=${roomPct} cy="12" r="3" stroke=${color}></circle>`
          : null}
      </svg>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'band-gauge': BandGauge;
  }
}
