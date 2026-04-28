/**
 * `<comfort-band-card-editor>` — visual config UI for the dashboard editor.
 *
 * Registered globally; HA picks it up via `static getConfigElement()` on
 * the card class. Renders a zone dropdown (populated from `comfort_band`
 * device identifiers) plus a "Compact mode" toggle.
 *
 * Fires HA's standard `config-changed` event with the merged config so the
 * dashboard preview re-renders live as the user picks a zone.
 */

import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { ComfortBandCardConfig, HomeAssistant } from './types.js';
import { tokens } from './styles.js';

@customElement('comfort-band-card-editor')
export class ComfortBandCardEditor extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @property({ attribute: false }) public config: ComfortBandCardConfig = {
    type: 'custom:comfort-band-card',
    zone: '',
  };

  public setConfig(config: ComfortBandCardConfig): void {
    this.config = {
      type: config.type ?? 'custom:comfort-band-card',
      zone: config.zone ?? '',
      ...(config.compact !== undefined ? { compact: config.compact } : {}),
    };
  }

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-bottom: var(--cb-gap-md);
        font-size: 12px;
        color: var(--cb-text-secondary);
      }
      select,
      input[type='checkbox'] {
        font: inherit;
      }
      select {
        font-size: 14px;
        padding: 8px;
        border: 1px solid var(--divider-color, #cccccc);
        border-radius: 6px;
        color: var(--cb-text-primary);
        background: var(--ha-card-background, #ffffff);
      }
      .checkbox-row {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 14px;
        color: var(--cb-text-primary);
      }
      .empty {
        color: var(--cb-text-secondary);
        font-size: 13px;
      }
    `,
  ];

  private _availableZones(): string[] {
    if (!this.hass) return [];
    const slugs: string[] = [];
    for (const device of Object.values(this.hass.devices)) {
      for (const [domain, key] of device.identifiers) {
        if (domain === 'comfort_band' && key.startsWith('zone:')) {
          slugs.push(key.slice('zone:'.length));
        }
      }
    }
    return slugs.sort();
  }

  private _onZoneChange = (event: Event) => {
    const zone = (event.target as HTMLSelectElement).value;
    this._fireConfig({ ...this.config, zone });
  };

  private _onCompactChange = (event: Event) => {
    const compact = (event.target as HTMLInputElement).checked;
    const next = { ...this.config };
    if (compact) next.compact = true;
    else delete next.compact;
    this._fireConfig(next);
  };

  private _fireConfig(config: ComfortBandCardConfig): void {
    this.config = config;
    this.dispatchEvent(
      new CustomEvent('config-changed', {
        detail: { config },
        bubbles: true,
        composed: true,
      }),
    );
  }

  protected override render() {
    const zones = this._availableZones();
    if (zones.length === 0) {
      return html`<div class="empty">
        No Comfort Band zones found. Add one via Settings → Devices & Services first.
      </div>`;
    }

    return html`
      <label>
        Zone
        <select @change=${this._onZoneChange} .value=${this.config.zone || ''}>
          ${this.config.zone === ''
            ? html`<option value="" disabled selected>Select a zone…</option>`
            : null}
          ${zones.map(
            (z) => html` <option value=${z} ?selected=${z === this.config.zone}>${z}</option> `,
          )}
        </select>
      </label>
      <label class="checkbox-row">
        <input
          type="checkbox"
          .checked=${this.config.compact === true}
          @change=${this._onCompactChange}
        />
        Compact mode (tile only, no expand on tap)
      </label>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-card-editor': ComfortBandCardEditor;
  }
}
