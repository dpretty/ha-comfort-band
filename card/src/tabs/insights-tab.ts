/**
 * `<comfort-band-insights-tab>` — Insights tab content.
 *
 * Wraps HA's built-in `history-graph` card via the well-known
 * `window.loadCardHelpers()` async loader (the canonical way for custom
 * cards to instantiate built-in card types). Falls back to a deep link
 * to HA's `/history` page if the helper isn't available (e.g. when the
 * frontend bundle is mid-update or the HA version predates loadCardHelpers).
 *
 * v0.2 will replace this with a custom uPlot chart that shades bands by
 * `current_action`. v0.1 just gives users a trend.
 */

import { LitElement, html, css, nothing } from 'lit';
import type { PropertyValues } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { ZoneEntities } from '../helpers.js';
import type { HomeAssistant } from '../types.js';
import { tokens } from '../styles.js';

interface CardHelpers {
  createCardElement: (config: unknown) => HTMLElement & { hass?: unknown };
}

declare global {
  interface Window {
    loadCardHelpers?: () => Promise<CardHelpers>;
  }
}

@customElement('comfort-band-insights-tab')
export class ComfortBandInsightsTab extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @property({ attribute: false }) public entities?: ZoneEntities;

  @state() private _graphAvailable: boolean | null = null;
  private _graphCard: (HTMLElement & { hass?: unknown }) | null = null;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .graph-container {
        min-height: 220px;
      }
      .graph-container :first-child {
        --ha-card-box-shadow: none;
      }
      .fallback,
      .empty {
        padding: var(--cb-gap-lg);
        color: var(--cb-text-secondary);
        font-size: 13px;
        text-align: center;
      }
      .fallback a {
        color: var(--cb-accent, var(--primary-color, #03a9f4));
        text-decoration: none;
        margin-left: 8px;
      }
    `,
  ];

  protected override async firstUpdated(): Promise<void> {
    await this._maybeMountGraph();
  }

  protected override updated(changed: PropertyValues<this>): void {
    if (changed.has('entities') || changed.has('hass')) {
      void this._maybeMountGraph();
    }
  }

  private async _maybeMountGraph(): Promise<void> {
    const entityId = this.entities?.roomTemperature;
    if (!entityId || !this.hass) return;

    if (this._graphCard) {
      this._graphCard.hass = this.hass;
      return;
    }

    if (typeof window.loadCardHelpers !== 'function') {
      this._graphAvailable = false;
      return;
    }

    try {
      const helpers = await window.loadCardHelpers();
      const card = helpers.createCardElement({
        type: 'history-graph',
        entities: [entityId],
        hours_to_show: 24,
      });
      card.hass = this.hass;
      const container = this.renderRoot.querySelector('.graph-container');
      if (container) {
        container.innerHTML = '';
        container.appendChild(card);
        this._graphCard = card;
        this._graphAvailable = true;
      }
    } catch {
      this._graphAvailable = false;
    }
  }

  protected override render() {
    const entityId = this.entities?.roomTemperature;
    if (!entityId) {
      return html`<div class="empty">No room temperature sensor for this zone.</div>`;
    }

    return html`
      <div class="graph-container"></div>
      ${this._graphAvailable === false
        ? html`<div class="fallback">
            Inline graph unavailable.
            <a href="/history?entity_id=${entityId}" target="_blank" rel="noopener"
              >Open in HA history →</a
            >
          </div>`
        : nothing}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-insights-tab': ComfortBandInsightsTab;
  }
}
