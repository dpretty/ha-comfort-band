/**
 * `<comfort-band-card>` — main custom element.
 *
 * Reads `{ type, zone }` config, resolves the zone's entities by device
 * identifier (sidesteps post-cutover entity_id chaos), and renders a
 * `<comfort-band-tile>` populated from live HA states. Tap on the tile
 * fires `comfort-band-tile-tap`; commit 4 will use that to open the modal.
 */

import { LitElement, html, css } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';
import './tile.js';
import './modal.js';
import './card-editor.js';
import type { ComfortBandModal } from './modal.js';
import type { ComfortBandCardConfig, HassEntity, HomeAssistant } from './types.js';
import type { ZoneEntities } from './helpers.js';
import { findZoneEntities } from './helpers.js';
import { tokens } from './styles.js';

@customElement('comfort-band-card')
export class ComfortBandCard extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @state() private _config?: ComfortBandCardConfig;
  @query('comfort-band-modal') private _modal?: ComfortBandModal;

  public setConfig(config: ComfortBandCardConfig): void {
    if (!config?.zone) {
      throw new Error('comfort-band-card: `zone` is required');
    }
    this._config = config;
  }

  /** HA's panel/grid uses this to size the card. ~1 row per ~50 px of content. */
  public getCardSize(): number {
    return 2;
  }

  /** HA dashboard editor: tells HA which custom element to render as the
   *  visual config UI. Returning the element directly (not a string) is
   *  the modern API. */
  public static getConfigElement(): HTMLElement {
    return document.createElement('comfort-band-card-editor');
  }

  /** Default config seed when the user adds the card via "Add card" in the
   *  dashboard editor. Picks the first registered Comfort Band zone, if any. */
  public static getStubConfig(hass: HomeAssistant | undefined): ComfortBandCardConfig {
    let zone = '';
    if (hass) {
      for (const device of Object.values(hass.devices)) {
        for (const [domain, key] of device.identifiers) {
          if (domain === 'comfort_band' && key.startsWith('zone:')) {
            zone = key.slice('zone:'.length);
            break;
          }
        }
        if (zone) break;
      }
    }
    return { type: 'custom:comfort-band-card', zone };
  }

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
      }
      .placeholder {
        padding: var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--ha-card-background, var(--card-background-color, #fff));
        color: var(--cb-text-secondary);
        font-family: var(--paper-font-body1_-_font-family, sans-serif);
        font-size: 13px;
      }
    `,
  ];

  protected override render() {
    if (!this._config || !this.hass) return html``;

    const zoneSlug = this._config.zone;
    const entities = findZoneEntities(this.hass, zoneSlug);

    if (entities.deviceId === null) {
      return html`<div class="placeholder">
        Comfort Band zone <code>${zoneSlug}</code> not found. Add it via Settings → Devices &
        Services.
      </div>`;
    }

    const compact = this._config.compact === true;
    const view = this._buildView(this.hass, entities);

    return html`
      <comfort-band-tile
        zoneName=${view.zoneName}
        .roomTemp=${view.roomTemp}
        .low=${view.low}
        .high=${view.high}
        .action=${view.action}
        .overrideActive=${view.overrideActive}
        .overrideEnds=${view.overrideEnds}
        .noExpand=${compact}
        @comfort-band-tile-tap=${this._onTileTap}
      ></comfort-band-tile>
      ${compact
        ? null
        : html`<comfort-band-modal
            .hass=${this.hass}
            zone=${zoneSlug}
            zoneName=${view.zoneName}
            .entities=${entities}
          ></comfort-band-modal>`}
    `;
  }

  private _onTileTap = () => {
    this._modal?.open();
  };

  private _buildView(hass: HomeAssistant, entities: ZoneEntities) {
    const stateOf = (entityId: string | null): HassEntity | undefined =>
      entityId !== null ? hass.states[entityId] : undefined;

    const numericState = (entityId: string | null): number => {
      const s = stateOf(entityId);
      if (!s) return NaN;
      const n = parseFloat(s.state);
      return Number.isFinite(n) ? n : NaN;
    };

    return {
      zoneName: entities.deviceName ?? this._config!.zone,
      low: numericState(entities.effectiveLow),
      high: numericState(entities.effectiveHigh),
      roomTemp: numericState(entities.roomTemperature),
      action: stateOf(entities.currentAction)?.state ?? 'unknown',
      overrideActive: stateOf(entities.overrideActive)?.state === 'on',
      overrideEnds: stateOf(entities.overrideEnds)?.state ?? null,
    };
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-card': ComfortBandCard;
  }
}
