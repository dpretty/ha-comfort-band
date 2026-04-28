/**
 * `<comfort-band-profiles-tab>` — Profiles tab content.
 *
 * Reads available profiles from the singleton
 * `select.comfort_band_profiles_active_profile` entity (via
 * `findActiveProfileEntity`). Selecting a profile fires
 * `comfort_band.set_profile`, which the integration's profile registry
 * dispatches to every zone coordinator.
 *
 * Profile create / clone / rename / delete are deferred to v0.2 (need
 * new comfort_band services that don't exist yet).
 */

import { LitElement, html, css, nothing } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import { findActiveProfileEntity } from '../helpers.js';
import { setProfile } from '../services.js';
import type { HomeAssistant } from '../types.js';
import { tokens } from '../styles.js';

@customElement('comfort-band-profiles-tab')
export class ComfortBandProfilesTab extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        padding: var(--cb-gap-md);
      }
      .empty {
        color: var(--cb-text-secondary);
        font-size: 13px;
        text-align: center;
        padding: var(--cb-gap-lg);
      }
      ul {
        list-style: none;
        padding: 0;
        margin: 0;
        display: flex;
        flex-direction: column;
        gap: var(--cb-gap-sm);
      }
      li {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: var(--cb-gap-sm) var(--cb-gap-md);
        border-radius: var(--cb-radius-card);
        background: var(--cb-track-bg);
        cursor: pointer;
        font-size: 14px;
        color: var(--cb-text-primary);
      }
      li.active {
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
      }
      li:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 2px;
      }
      .name {
        font-weight: 500;
        text-transform: capitalize;
      }
      .badge {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.04em;
        opacity: 0.85;
      }
      .footer {
        margin-top: var(--cb-gap-md);
        font-size: 12px;
        color: var(--cb-text-secondary);
        text-align: center;
      }
    `,
  ];

  private _onSelect(profile: string) {
    if (!this.hass) return;
    void setProfile(this.hass, { profile });
  }

  protected override render() {
    if (!this.hass) return nothing;
    const entityId = findActiveProfileEntity(this.hass);
    if (entityId === null) {
      return html`<div class="empty">Profile manager not registered yet.</div>`;
    }
    const state = this.hass.states[entityId];
    const optionsAttr = state?.attributes.options;
    const options = Array.isArray(optionsAttr)
      ? (optionsAttr as unknown[]).filter((v): v is string => typeof v === 'string')
      : [];
    const active = state?.state ?? '';

    if (options.length === 0) {
      return html`<div class="empty">No profiles configured.</div>`;
    }

    return html`
      <ul role="listbox" aria-label="Profiles">
        ${options.map(
          (profile) => html`
            <li
              role="option"
              tabindex="0"
              class=${profile === active ? 'active' : ''}
              aria-selected=${profile === active}
              @click=${() => this._onSelect(profile)}
              @keydown=${(e: KeyboardEvent) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault();
                  this._onSelect(profile);
                }
              }}
            >
              <span class="name">${profile}</span>
              ${profile === active ? html`<span class="badge">Active</span>` : nothing}
            </li>
          `,
        )}
      </ul>
      <div class="footer">Create / rename / delete profiles in a future release.</div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-profiles-tab': ComfortBandProfilesTab;
  }
}
