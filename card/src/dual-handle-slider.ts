/**
 * `<dual-handle-slider>` — single-track range with two draggable thumbs.
 *
 * Used for the manual low/high in the Now tab. Behaviour:
 *   - Dragging the low thumb writes `low`; clamps to `[min, high - step]`.
 *   - Dragging the high thumb writes `high`; clamps to `[low + step, max]`.
 *   - Fires `input` continuously while dragging, `change` on release or
 *     after a keyboard adjustment. Both events have `detail = {low, high}`.
 *   - Keyboard: Arrow keys step, Home/End jump to bounds.
 */

import { LitElement, html, css } from 'lit';
import { customElement, property, query, state } from 'lit/decorators.js';
import { tokens } from './styles.js';

type Handle = 'low' | 'high';

@customElement('dual-handle-slider')
export class DualHandleSlider extends LitElement {
  @property({ type: Number }) public min = 16;
  @property({ type: Number }) public max = 26;
  @property({ type: Number }) public step = 0.5;
  @property({ type: Number }) public low = 19;
  @property({ type: Number }) public high = 22;
  @property({ type: String }) public unit = '°';

  @state() private _dragging: Handle | null = null;

  @query('.track') private _track?: HTMLElement;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
        padding: 16px 12px;
        --thumb-size: 20px;
      }
      .track {
        position: relative;
        height: 6px;
        background: var(--cb-track-bg);
        border-radius: 3px;
        cursor: pointer;
      }
      .fill {
        position: absolute;
        top: 0;
        height: 100%;
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        opacity: 0.6;
        border-radius: 3px;
        pointer-events: none;
      }
      .thumb {
        position: absolute;
        top: 50%;
        width: var(--thumb-size);
        height: var(--thumb-size);
        margin-left: calc(var(--thumb-size) / -2);
        margin-top: calc(var(--thumb-size) / -2);
        background: var(--ha-card-background, #ffffff);
        border: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        border-radius: 50%;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
        cursor: grab;
        touch-action: none;
        transition: transform 0.1s ease;
      }
      .thumb:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 3px;
      }
      .thumb.dragging {
        cursor: grabbing;
        transform: scale(1.15);
      }
      .label-row {
        display: flex;
        justify-content: space-between;
        font-size: 12px;
        color: var(--cb-text-secondary);
        margin-top: 14px;
        font-variant-numeric: tabular-nums;
      }
      .value-low,
      .value-high {
        font-size: 14px;
        font-weight: 500;
        color: var(--cb-text-primary);
      }
    `,
  ];

  private _pct(value: number): number {
    const range = this.max - this.min;
    if (range <= 0) return 0;
    return ((value - this.min) / range) * 100;
  }

  private _snap(value: number): number {
    const stepped = Math.round((value - this.min) / this.step) * this.step + this.min;
    return Math.max(this.min, Math.min(this.max, stepped));
  }

  private _setHandle(handle: Handle, raw: number): boolean {
    const value = this._snap(raw);
    if (handle === 'low') {
      const clamped = Math.min(value, this.high - this.step);
      if (clamped === this.low) return false;
      this.low = clamped;
    } else {
      const clamped = Math.max(value, this.low + this.step);
      if (clamped === this.high) return false;
      this.high = clamped;
    }
    return true;
  }

  private _xToValue(clientX: number): number {
    const rect = this._track?.getBoundingClientRect();
    if (!rect || rect.width === 0) return this.min;
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return this.min + ratio * (this.max - this.min);
  }

  private _onThumbPointerDown = (event: PointerEvent, handle: Handle) => {
    event.preventDefault();
    const target = event.currentTarget as HTMLElement;
    target.setPointerCapture(event.pointerId);
    this._dragging = handle;

    const onMove = (ev: PointerEvent) => {
      if (this._setHandle(handle, this._xToValue(ev.clientX))) {
        this._fire('input');
      }
    };
    const onUp = (ev: PointerEvent) => {
      target.releasePointerCapture(ev.pointerId);
      target.removeEventListener('pointermove', onMove);
      target.removeEventListener('pointerup', onUp);
      target.removeEventListener('pointercancel', onUp);
      this._dragging = null;
      this._fire('change');
    };
    target.addEventListener('pointermove', onMove);
    target.addEventListener('pointerup', onUp);
    target.addEventListener('pointercancel', onUp);
  };

  private _onTrackPointerDown = (event: PointerEvent) => {
    if ((event.target as HTMLElement).classList.contains('thumb')) return;
    const value = this._xToValue(event.clientX);
    const mid = (this.low + this.high) / 2;
    const handle: Handle = value < mid ? 'low' : 'high';
    if (this._setHandle(handle, value)) this._fire('change');
  };

  private _onKeyDown = (event: KeyboardEvent, handle: Handle) => {
    let delta = 0;
    switch (event.key) {
      case 'ArrowLeft':
      case 'ArrowDown':
        delta = -this.step;
        break;
      case 'ArrowRight':
      case 'ArrowUp':
        delta = this.step;
        break;
      case 'Home':
        event.preventDefault();
        if (this._setHandle(handle, this.min)) this._fire('change');
        return;
      case 'End':
        event.preventDefault();
        if (this._setHandle(handle, this.max)) this._fire('change');
        return;
      default:
        return;
    }
    event.preventDefault();
    const current = handle === 'low' ? this.low : this.high;
    if (this._setHandle(handle, current + delta)) this._fire('change');
  };

  private _fire(type: 'input' | 'change') {
    this.dispatchEvent(
      new CustomEvent(type, {
        detail: { low: this.low, high: this.high },
        bubbles: true,
        composed: true,
      }),
    );
  }

  private _fmt(value: number): string {
    return `${value.toFixed(1)}${this.unit}`;
  }

  protected override render() {
    const lowPct = this._pct(this.low);
    const highPct = this._pct(this.high);
    return html`
      <div class="track" @pointerdown=${this._onTrackPointerDown}>
        <div class="fill" style="left:${lowPct}%; width:${highPct - lowPct}%"></div>
        <div
          class="thumb ${this._dragging === 'low' ? 'dragging' : ''}"
          style="left:${lowPct}%"
          tabindex="0"
          role="slider"
          aria-label="Lower bound"
          aria-valuemin=${this.min}
          aria-valuemax=${this.high - this.step}
          aria-valuenow=${this.low}
          aria-valuetext=${this._fmt(this.low)}
          @pointerdown=${(e: PointerEvent) => this._onThumbPointerDown(e, 'low')}
          @keydown=${(e: KeyboardEvent) => this._onKeyDown(e, 'low')}
        ></div>
        <div
          class="thumb ${this._dragging === 'high' ? 'dragging' : ''}"
          style="left:${highPct}%"
          tabindex="0"
          role="slider"
          aria-label="Upper bound"
          aria-valuemin=${this.low + this.step}
          aria-valuemax=${this.max}
          aria-valuenow=${this.high}
          aria-valuetext=${this._fmt(this.high)}
          @pointerdown=${(e: PointerEvent) => this._onThumbPointerDown(e, 'high')}
          @keydown=${(e: KeyboardEvent) => this._onKeyDown(e, 'high')}
        ></div>
      </div>
      <div class="label-row">
        <span class="value-low">${this._fmt(this.low)}</span>
        <span class="value-high">${this._fmt(this.high)}</span>
      </div>
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'dual-handle-slider': DualHandleSlider;
  }
}
