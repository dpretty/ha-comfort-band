/**
 * `<transition-edit-dialog>` — inline form for adding or editing a single
 * schedule transition.
 *
 * Renders inside the schedule-tab (not its own modal) — the parent modal
 * already owns the `<dialog>` element; nesting modals causes z-index pain
 * and is awkward for keyboard focus. This element is a controlled form:
 *   - Set `transition` to the row being edited (or null to add new).
 *   - Listens for `save` / `cancel` / `delete` button clicks; emits
 *     `dialog-save` { transition } / `dialog-cancel` / `dialog-delete` { at }.
 */

import { LitElement, html, css } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';
import type { Transition } from './types.js';
import { tokens } from './styles.js';

@customElement('transition-edit-dialog')
export class TransitionEditDialog extends LitElement {
  @property({ type: Object }) public transition: Transition | null = null;
  /** True when adding a new transition; false when editing existing. */
  @property({ type: Boolean }) public isNew = false;

  @state() private _at = '06:00';
  @state() private _low = 19;
  @state() private _high = 22;
  @state() private _error: string | null = null;

  @query('input[name="at"]') private _atInput?: HTMLInputElement;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        padding: var(--cb-gap-md);
        background: var(--ha-card-background, #ffffff);
        border-radius: var(--cb-radius-card);
      }
      h3 {
        margin: 0 0 var(--cb-gap-md);
        font-size: 14px;
        font-weight: 500;
      }
      label {
        display: flex;
        flex-direction: column;
        gap: 4px;
        margin-bottom: var(--cb-gap-md);
        font-size: 12px;
        color: var(--cb-text-secondary);
      }
      input {
        font: inherit;
        font-size: 14px;
        padding: 8px;
        border: 1px solid var(--divider-color, #cccccc);
        border-radius: 6px;
        color: var(--cb-text-primary);
        background: transparent;
      }
      input:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 1px;
      }
      .error {
        color: var(--error-color, #b71c1c);
        font-size: 12px;
        margin-bottom: var(--cb-gap-sm);
      }
      .actions {
        display: flex;
        justify-content: flex-end;
        gap: var(--cb-gap-sm);
      }
      .actions .spacer {
        flex: 1;
      }
      .button {
        font: inherit;
        padding: 6px 12px;
        border-radius: var(--cb-radius-pill);
        border: 1px solid transparent;
        cursor: pointer;
        font-size: 13px;
      }
      .button.primary {
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        color: #ffffff;
      }
      .button.secondary {
        background: transparent;
        border-color: var(--divider-color, #cccccc);
        color: var(--cb-text-primary);
      }
      .button.danger {
        background: transparent;
        color: var(--error-color, #b71c1c);
        border-color: var(--divider-color, #cccccc);
      }
    `,
  ];

  protected override willUpdate(changed: Map<string | number | symbol, unknown>): void {
    if (changed.has('transition') && this.transition) {
      this._at = this.transition.at;
      this._low = this.transition.low;
      this._high = this.transition.high;
      this._error = null;
    }
  }

  protected override firstUpdated(): void {
    queueMicrotask(() => this._atInput?.focus());
  }

  private _onSave = () => {
    if (!this._validate()) return;
    this.dispatchEvent(
      new CustomEvent('dialog-save', {
        detail: { transition: { at: this._at, low: this._low, high: this._high } },
        bubbles: true,
        composed: true,
      }),
    );
  };

  private _onCancel = () => {
    this.dispatchEvent(new CustomEvent('dialog-cancel', { bubbles: true, composed: true }));
  };

  private _onDelete = () => {
    if (!this.transition) return;
    this.dispatchEvent(
      new CustomEvent('dialog-delete', {
        detail: { at: this.transition.at },
        bubbles: true,
        composed: true,
      }),
    );
  };

  private _validate(): boolean {
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(this._at)) {
      this._error = 'Time must be HH:MM (24h, e.g. 06:00).';
      return false;
    }
    if (Number.isNaN(this._low) || Number.isNaN(this._high)) {
      this._error = 'Low and high must be numbers.';
      return false;
    }
    if (this._low >= this._high) {
      this._error = 'Low must be less than high.';
      return false;
    }
    if (this._low < 10 || this._high > 35) {
      this._error = 'Temperatures must be between 10 °C and 35 °C.';
      return false;
    }
    this._error = null;
    return true;
  }

  protected override render() {
    return html`
      <h3>${this.isNew ? 'Add transition' : 'Edit transition'}</h3>
      <label>
        Time (HH:MM)
        <input
          name="at"
          type="text"
          inputmode="numeric"
          .value=${this._at}
          @input=${(e: Event) => (this._at = (e.target as HTMLInputElement).value)}
        />
      </label>
      <label>
        Low (°C)
        <input
          name="low"
          type="number"
          step="0.5"
          min="10"
          max="35"
          .value=${String(this._low)}
          @input=${(e: Event) => (this._low = parseFloat((e.target as HTMLInputElement).value))}
        />
      </label>
      <label>
        High (°C)
        <input
          name="high"
          type="number"
          step="0.5"
          min="10"
          max="35"
          .value=${String(this._high)}
          @input=${(e: Event) => (this._high = parseFloat((e.target as HTMLInputElement).value))}
        />
      </label>
      ${this._error ? html`<div class="error">${this._error}</div>` : null}
      <div class="actions">
        ${this.isNew
          ? null
          : html`<button class="button danger" @click=${this._onDelete}>Delete</button>`}
        <div class="spacer"></div>
        <button class="button secondary" @click=${this._onCancel}>Cancel</button>
        <button class="button primary" @click=${this._onSave}>Save</button>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'transition-edit-dialog': TransitionEditDialog;
  }
}
