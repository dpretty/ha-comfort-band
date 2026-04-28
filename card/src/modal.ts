/**
 * `<comfort-band-modal>` — full-screen tabbed dialog opened by tile tap.
 *
 * Uses the native `<dialog>` element (modal API): `showModal()` opens with
 * a backdrop, ESC and click-outside close it. No browser_mod dependency.
 *
 * Tabs in v0.1.0:
 *   - Now (commit 4)
 *   - Schedule (commit 7)
 *   - Profiles (commit 5)
 *   - Insights (commit 6)
 *
 * Tabs the user hasn't reached yet render a "coming soon" placeholder so
 * the modal works end-to-end through every commit.
 */

import { LitElement, html, css, nothing } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';
import './tabs/now-tab.js';
import './tabs/profiles-tab.js';
import './tabs/insights-tab.js';
import './tabs/schedule-tab.js';
import type { ZoneEntities } from './helpers.js';
import type { HomeAssistant } from './types.js';
import { tokens } from './styles.js';

type TabId = 'now' | 'schedule' | 'profiles' | 'insights';

const TABS: ReadonlyArray<{ id: TabId; label: string }> = [
  { id: 'now', label: 'Now' },
  { id: 'schedule', label: 'Schedule' },
  { id: 'profiles', label: 'Profiles' },
  { id: 'insights', label: 'Insights' },
];

@customElement('comfort-band-modal')
export class ComfortBandModal extends LitElement {
  @property({ attribute: false }) public hass?: HomeAssistant;
  @property({ type: String }) public zone = '';
  @property({ type: String }) public zoneName = '';
  @property({ attribute: false }) public entities?: ZoneEntities;

  @state() private _activeTab: TabId = 'now';
  @state() private _isOpen = false;

  @query('dialog') private _dialog?: HTMLDialogElement;

  public static override styles = [
    tokens,
    css`
      :host {
        --cb-modal-max-width: 480px;
      }
      dialog {
        width: min(90vw, var(--cb-modal-max-width));
        max-height: min(90vh, 720px);
        padding: 0;
        border: none;
        border-radius: var(--cb-radius-card);
        background: var(--ha-card-background, var(--card-background-color, #ffffff));
        color: var(--cb-text-primary);
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.18);
        overflow: hidden;
      }
      dialog::backdrop {
        background: rgba(0, 0, 0, 0.4);
      }
      .frame {
        display: flex;
        flex-direction: column;
        max-height: inherit;
      }
      header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: var(--cb-gap-md);
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
      }
      header h2 {
        margin: 0;
        font-size: 16px;
        font-weight: 500;
        color: var(--cb-text-primary);
      }
      .close {
        font: inherit;
        font-size: 22px;
        line-height: 1;
        background: transparent;
        border: none;
        color: var(--cb-text-secondary);
        cursor: pointer;
        padding: 4px 8px;
      }
      nav {
        display: flex;
        gap: 0;
        border-bottom: 1px solid var(--divider-color, #e0e0e0);
        overflow-x: auto;
      }
      nav button {
        font: inherit;
        font-size: 13px;
        padding: 10px 14px;
        background: transparent;
        border: none;
        border-bottom: 2px solid transparent;
        color: var(--cb-text-secondary);
        cursor: pointer;
        white-space: nowrap;
      }
      nav button[aria-selected='true'] {
        color: var(--cb-accent, var(--primary-color, #03a9f4));
        border-bottom-color: var(--cb-accent, var(--primary-color, #03a9f4));
      }
      .panel {
        overflow-y: auto;
        flex: 1;
      }
      .placeholder {
        padding: var(--cb-gap-lg);
        color: var(--cb-text-secondary);
        font-size: 13px;
        text-align: center;
      }
    `,
  ];

  public open(): void {
    this._isOpen = true;
    this.updateComplete.then(() => this._dialog?.showModal());
  }

  public close(): void {
    this._dialog?.close();
  }

  public selectTab(tab: TabId): void {
    this._activeTab = tab;
  }

  private _onClose = () => {
    this._isOpen = false;
    this.dispatchEvent(
      new CustomEvent('comfort-band-modal-close', { bubbles: true, composed: true }),
    );
  };

  private _onSelectTab = (tab: TabId) => {
    this._activeTab = tab;
  };

  protected override render() {
    if (!this._isOpen) return nothing;
    const title = this.zoneName || this.zone || 'Comfort Band';
    return html`
      <dialog @close=${this._onClose}>
        <div class="frame">
          <header>
            <h2>${title}</h2>
            <button class="close" @click=${this.close} aria-label="Close">×</button>
          </header>
          <nav role="tablist">
            ${TABS.map(
              (t) => html`
                <button
                  role="tab"
                  aria-selected=${this._activeTab === t.id}
                  @click=${() => this._onSelectTab(t.id)}
                >
                  ${t.label}
                </button>
              `,
            )}
          </nav>
          <div class="panel" role="tabpanel">${this._renderTab()}</div>
        </div>
      </dialog>
    `;
  }

  private _renderTab() {
    switch (this._activeTab) {
      case 'now':
        return html`<comfort-band-now-tab
          .hass=${this.hass}
          .zone=${this.zone}
          .entities=${this.entities}
        ></comfort-band-now-tab>`;
      case 'schedule':
        return html`<comfort-band-schedule-tab
          .hass=${this.hass}
          .zone=${this.zone}
        ></comfort-band-schedule-tab>`;
      case 'profiles':
        return html`<comfort-band-profiles-tab .hass=${this.hass}></comfort-band-profiles-tab>`;
      case 'insights':
        return html`<comfort-band-insights-tab
          .hass=${this.hass}
          .entities=${this.entities}
        ></comfort-band-insights-tab>`;
    }
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'comfort-band-modal': ComfortBandModal;
  }
}
