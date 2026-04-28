import { LitElement, html, css } from 'lit';
import { customElement, property, state } from 'lit/decorators.js';
import type { ComfortBandCardConfig, HomeAssistant } from './types.js';

@customElement('comfort-band-card')
export class ComfortBandCard extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @state() private _config?: ComfortBandCardConfig;

  public setConfig(config: ComfortBandCardConfig): void {
    if (!config?.zone) {
      throw new Error('comfort-band-card: `zone` is required');
    }
    this._config = config;
  }

  public static override styles = css`
    :host {
      display: block;
      padding: 12px;
      border-radius: 12px;
      background: var(--ha-card-background, var(--card-background-color, #fff));
      box-shadow: var(--ha-card-box-shadow, none);
    }
    .stub {
      color: var(--primary-text-color, #000);
      font-family: var(--paper-font-body1_-_font-family, sans-serif);
    }
  `;

  protected override render() {
    if (!this._config) return html``;
    return html`<div class="stub">comfort-band-card · zone: ${this._config.zone}</div>`;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-card': ComfortBandCard;
  }
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
  '%c COMFORT-BAND-CARD %c v0.1.0-dev ',
  'color:white;background:#2196F3;padding:2px 4px;border-radius:3px',
  'color:#000;background:#fff;padding:2px 4px;border-radius:3px',
);
