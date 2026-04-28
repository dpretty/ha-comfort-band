/**
 * `<comfort-band-tile>` — compact tile (~120 px tall).
 *
 * Composed of zone display name + prominent room temperature + horizontal
 * `<band-gauge>` + an override pill (when active). Tap fires
 * `comfort-band-tile-tap`; the parent card opens the modal in response.
 */

import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import './band-gauge.js';
import { tokens, actionColorVar, asAction, actionLabel } from './styles.js';

@customElement('comfort-band-tile')
export class ComfortBandTile extends LitElement {
  @property({ type: String }) public zoneName = '';
  @property({ type: Number }) public roomTemp = NaN;
  @property({ type: Number }) public low = NaN;
  @property({ type: Number }) public high = NaN;
  @property({ type: String }) public action: string | null = 'unknown';
  @property({ type: Boolean }) public overrideActive = false;
  /** ISO 8601 timestamp at which the override expires (or null). */
  @property({ type: String }) public overrideEnds: string | null = null;
  /** Disable the tap-to-expand affordance. Used for compact embeds. */
  @property({ type: Boolean }) public noExpand = false;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
      }
      .tile {
        display: flex;
        flex-direction: column;
        gap: var(--cb-gap-sm);
        padding: var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--ha-card-background, var(--card-background-color, #ffffff));
        box-shadow: var(--ha-card-box-shadow, none);
        cursor: pointer;
        transition: transform 0.12s ease;
      }
      .tile.no-expand {
        cursor: default;
      }
      .tile:not(.no-expand):hover {
        transform: translateY(-1px);
      }
      .tile:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 2px;
      }
      .header {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: var(--cb-gap-sm);
      }
      .zone-name {
        font-size: 14px;
        font-weight: 500;
        color: var(--cb-text-primary);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
      }
      .action-chip {
        font-size: 11px;
        font-weight: 500;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        padding: 2px 8px;
        border-radius: var(--cb-radius-pill);
        color: var(--cb-text-on-action, #ffffff);
      }
      .body {
        display: flex;
        align-items: center;
        gap: var(--cb-gap-md);
      }
      .room-temp {
        font-size: 32px;
        font-weight: 300;
        color: var(--cb-text-primary);
        font-variant-numeric: tabular-nums;
        line-height: 1;
        min-width: 70px;
      }
      .gauge-wrap {
        flex: 1;
        min-width: 0;
      }
      .override-pill {
        align-self: flex-start;
        font-size: 11px;
        padding: 2px 8px;
        border-radius: var(--cb-radius-pill);
        background: var(--cb-text-secondary);
        color: var(--cb-text-on-action, #ffffff);
        opacity: 0.85;
      }
    `,
  ];

  private _onTap(event: MouseEvent | KeyboardEvent) {
    if (this.noExpand) return;
    if (event instanceof KeyboardEvent && event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    this.dispatchEvent(new CustomEvent('comfort-band-tile-tap', { bubbles: true, composed: true }));
  }

  private _renderRoomTemp(): string {
    return Number.isFinite(this.roomTemp) ? `${this.roomTemp.toFixed(1)}°` : '—';
  }

  private _renderOverridePill() {
    if (!this.overrideActive) return null;
    const remaining = formatRemaining(this.overrideEnds);
    return html`<div class="override-pill">Override${remaining ? ` · ${remaining}` : ''}</div>`;
  }

  private _renderActionChip() {
    const action = asAction(this.action);
    if (action === 'idle' || action === 'unknown') return null;
    const color = actionColorVar(action);
    return html`<span class="action-chip" style="background:${color}">
      ${actionLabel(action)}
    </span>`;
  }

  protected override render() {
    return html`
      <div
        class="tile ${this.noExpand ? 'no-expand' : ''}"
        role="${this.noExpand ? 'group' : 'button'}"
        tabindex="${this.noExpand ? -1 : 0}"
        @click=${this._onTap}
        @keydown=${this._onTap}
      >
        <div class="header">
          <div class="zone-name">${this.zoneName || '—'}</div>
          ${this._renderActionChip()}
        </div>
        <div class="body">
          <div class="room-temp">${this._renderRoomTemp()}</div>
          <div class="gauge-wrap">
            <band-gauge
              .low=${this.low}
              .high=${this.high}
              .room=${this.roomTemp}
              .action=${this.action}
            ></band-gauge>
          </div>
        </div>
        ${this._renderOverridePill()}
      </div>
    `;
  }
}

/** Returns "1h 23m left" / "12m left" / "" for non-future timestamps. */
function formatRemaining(iso: string | null): string {
  if (!iso) return '';
  const target = Date.parse(iso);
  if (Number.isNaN(target)) return '';
  const ms = target - Date.now();
  if (ms <= 0) return '';
  const totalMins = Math.round(ms / 60_000);
  if (totalMins < 60) return `${totalMins}m left`;
  const hours = Math.floor(totalMins / 60);
  const mins = totalMins % 60;
  return mins ? `${hours}h ${mins}m left` : `${hours}h left`;
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-tile': ComfortBandTile;
  }
}
