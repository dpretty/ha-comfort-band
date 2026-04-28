/**
 * `<comfort-band-now-tab>` — the "Now" tab content of the modal.
 *
 * Reads live state for the zone (room, low, high, action, override) and
 * shows:
 *   - Big band gauge at the top.
 *   - Current action chip (heating / cooling / idle).
 *   - Dual-handle slider for manual_low/manual_high — any change starts
 *     an override via `comfort_band.start_override` (the slider's
 *     `change` event, fired on release, drives the service call).
 *   - Override-active state line + Cancel-override button.
 *   - Override-duration buttons (1h / 3h / 6h) that update the
 *     `number.{zone}_override_hours` entity for future overrides.
 */

import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import '../band-gauge.js';
import '../dual-handle-slider.js';
import type { DualHandleSlider } from '../dual-handle-slider.js';
import type { ZoneEntities } from '../helpers.js';
import type { HomeAssistant } from '../types.js';
import { cancelOverride, startOverride } from '../services.js';
import { tokens, actionColorVar, asAction, actionLabel } from '../styles.js';

const HOUR_PRESETS = [1, 3, 6] as const;

@customElement('comfort-band-now-tab')
export class ComfortBandNowTab extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @property({ type: String }) public zone = '';
  @property({ attribute: false }) public entities?: ZoneEntities;

  @state() private _pendingLow: number | null = null;
  @state() private _pendingHigh: number | null = null;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .gauge-row {
        margin-bottom: var(--cb-gap-md);
      }
      .header-row {
        display: flex;
        align-items: baseline;
        gap: var(--cb-gap-sm);
        margin-bottom: var(--cb-gap-sm);
      }
      .room-temp {
        font-size: 36px;
        font-weight: 300;
        color: var(--cb-text-primary);
        font-variant-numeric: tabular-nums;
        line-height: 1;
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
      section {
        margin-top: var(--cb-gap-lg);
      }
      h3 {
        margin: 0 0 var(--cb-gap-sm);
        font-size: 13px;
        font-weight: 500;
        color: var(--cb-text-secondary);
        text-transform: uppercase;
        letter-spacing: 0.04em;
      }
      .override-row {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: var(--cb-gap-sm);
        padding: var(--cb-gap-sm) var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--cb-track-bg);
        font-size: 13px;
        color: var(--cb-text-primary);
      }
      .button {
        font: inherit;
        padding: 6px 12px;
        border-radius: var(--cb-radius-pill);
        border: 1px solid transparent;
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
        cursor: pointer;
      }
      .button.secondary {
        background: transparent;
        border-color: var(--divider-color, #cccccc);
        color: var(--cb-text-primary);
      }
      .preset-row {
        display: flex;
        gap: var(--cb-gap-sm);
      }
      .preset {
        font: inherit;
        padding: 4px 10px;
        border-radius: var(--cb-radius-pill);
        border: 1px solid var(--divider-color, #cccccc);
        background: transparent;
        color: var(--cb-text-primary);
        cursor: pointer;
      }
      .preset.active {
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
        border-color: transparent;
      }
    `,
  ];

  private get _stateOf() {
    const states = this.hass?.states ?? {};
    return (entityId: string | null) => (entityId !== null ? states[entityId] : undefined);
  }

  private _numericState(entityId: string | null): number {
    const s = this._stateOf(entityId);
    if (!s) return NaN;
    const n = parseFloat(s.state);
    return Number.isFinite(n) ? n : NaN;
  }

  private _onSliderInput = (event: CustomEvent<{ low: number; high: number }>) => {
    this._pendingLow = event.detail.low;
    this._pendingHigh = event.detail.high;
  };

  private _onSliderChange = (event: CustomEvent<{ low: number; high: number }>) => {
    if (!this.hass || !this.zone) return;
    this._pendingLow = null;
    this._pendingHigh = null;
    void startOverride(this.hass, {
      zone: this.zone,
      low: event.detail.low,
      high: event.detail.high,
    });
  };

  private _onCancel = () => {
    if (!this.hass || !this.zone) return;
    void cancelOverride(this.hass, { zone: this.zone });
  };

  private _onPickHours = (hours: number) => {
    if (!this.hass || !this.entities?.overrideHours) return;
    void this.hass.callService('number', 'set_value', {
      entity_id: this.entities.overrideHours,
      value: hours,
    });
  };

  protected override render() {
    if (!this.hass || !this.entities) return nothing;

    const lowState = this._numericState(this.entities.manualLow);
    const highState = this._numericState(this.entities.manualHigh);
    const effLow = this._numericState(this.entities.effectiveLow);
    const effHigh = this._numericState(this.entities.effectiveHigh);
    const room = this._numericState(this.entities.roomTemperature);
    const overrideHours = this._numericState(this.entities.overrideHours);
    const action = this._stateOf(this.entities.currentAction)?.state ?? 'unknown';
    const overrideActive = this._stateOf(this.entities.overrideActive)?.state === 'on';

    const sliderLow = this._pendingLow ?? (Number.isFinite(lowState) ? lowState : 19);
    const sliderHigh = this._pendingHigh ?? (Number.isFinite(highState) ? highState : 22);

    const actionTyped = asAction(action);
    const showChip = actionTyped !== 'idle' && actionTyped !== 'unknown';

    return html`
      <div class="header-row">
        <div class="room-temp">${Number.isFinite(room) ? `${room.toFixed(1)}°` : '—'}</div>
        ${showChip
          ? html`<span class="action-chip" style="background:${actionColorVar(actionTyped)}"
              >${actionLabel(actionTyped)}</span
            >`
          : nothing}
      </div>
      <div class="gauge-row">
        <band-gauge .low=${effLow} .high=${effHigh} .room=${room} .action=${action}></band-gauge>
      </div>

      <section>
        <h3>Manual band</h3>
        <dual-handle-slider
          .min=${16}
          .max=${26}
          .step=${0.5}
          .low=${sliderLow}
          .high=${sliderHigh}
          @input=${this._onSliderInput}
          @change=${this._onSliderChange}
        ></dual-handle-slider>
      </section>

      ${this._renderOverrideSection(overrideActive)} ${this._renderHoursSection(overrideHours)}
    `;
  }

  private _renderOverrideSection(overrideActive: boolean) {
    if (!overrideActive) return nothing;
    const ends = this._stateOf(this.entities!.overrideEnds)?.state;
    const remaining = formatRemaining(ends ?? null);
    return html`
      <section>
        <h3>Override</h3>
        <div class="override-row">
          <span>Active${remaining ? ` · ${remaining}` : ''}</span>
          <button class="button secondary" @click=${this._onCancel}>Cancel</button>
        </div>
      </section>
    `;
  }

  private _renderHoursSection(overrideHours: number) {
    if (!this.entities?.overrideHours) return nothing;
    return html`
      <section>
        <h3>Override duration</h3>
        <div class="preset-row">
          ${HOUR_PRESETS.map(
            (h) => html`
              <button
                class="preset ${overrideHours === h ? 'active' : ''}"
                @click=${() => this._onPickHours(h)}
              >
                ${h} h
              </button>
            `,
          )}
        </div>
      </section>
    `;
  }
}

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

// Avoid unused-import lint complaints.
export type { DualHandleSlider };

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-now-tab': ComfortBandNowTab;
  }
}
