/**
 * `<timeline-editor>` — 24-hour horizontal ribbon of schedule transitions.
 *
 * Events emitted (all bubble, composed=true):
 *   - `transition-add`    { at: 'HH:MM' }   — user tapped empty space.
 *   - `transition-edit`   { transition }     — user tapped an existing point.
 *   - `transition-delete` { at: 'HH:MM' }    — user long-pressed a point.
 *
 * Drag-to-move-time is deferred to v0.2 — for now, time changes go through
 * the precise-edit dialog. This keeps interactions unambiguous and tests
 * tractable. Tap targets get a 12 px hit-radius (per parent plan).
 */

import { LitElement, html, css } from 'lit';
import { customElement, property } from 'lit/decorators.js';
import type { Transition } from './types.js';
import { tokens } from './styles.js';

const LONG_PRESS_MS = 500;
const TAP_HIT_RADIUS_PCT = 1.5; // ~12 px on a 800 px wide track
const SNAP_MINUTES = 15;
const HOUR_LABELS = [0, 6, 12, 18, 24];

function timeStringToMinutes(at: string): number {
  const match = /^(\d{1,2}):(\d{2})$/.exec(at);
  if (!match) return 0;
  return parseInt(match[1], 10) * 60 + parseInt(match[2], 10);
}

function minutesToTimeString(mins: number): string {
  const clamped = Math.max(0, Math.min(24 * 60 - 1, mins));
  const h = Math.floor(clamped / 60);
  const m = clamped % 60;
  return `${h.toString().padStart(2, '0')}:${m.toString().padStart(2, '0')}`;
}

function minutesToPct(mins: number): number {
  return (mins / (24 * 60)) * 100;
}

function snapMinutes(mins: number, step: number): number {
  return Math.round(mins / step) * step;
}

@customElement('timeline-editor')
export class TimelineEditor extends LitElement {
  @property({ type: Array }) public transitions: Transition[] = [];

  private _longPressTimer: number | null = null;

  public static override styles = [
    tokens,
    css`
      :host {
        display: block;
      }
      .ruler {
        position: relative;
        height: 80px;
        margin: var(--cb-gap-md) 0;
      }
      .track {
        position: absolute;
        top: 36px;
        left: 0;
        right: 0;
        height: 6px;
        background: var(--cb-track-bg);
        border-radius: 3px;
        cursor: pointer;
      }
      .hour-tick {
        position: absolute;
        top: 18px;
        font-size: 10px;
        color: var(--cb-text-secondary);
        transform: translateX(-50%);
        font-variant-numeric: tabular-nums;
      }
      .hour-tick.terminal {
        transform: translateX(-100%);
      }
      .hour-tick.start {
        transform: translateX(0);
      }
      .point {
        position: absolute;
        top: 30px;
        width: 18px;
        height: 18px;
        margin-left: -9px;
        background: var(--cb-accent, var(--primary-color, #03a9f4));
        border: 2px solid var(--ha-card-background, #ffffff);
        border-radius: 50%;
        cursor: pointer;
        touch-action: none;
        z-index: 1;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
      }
      .point:focus-visible {
        outline: 2px solid var(--cb-accent, var(--primary-color, #03a9f4));
        outline-offset: 3px;
      }
      .point-label {
        position: absolute;
        top: 56px;
        font-size: 10px;
        color: var(--cb-text-secondary);
        transform: translateX(-50%);
        white-space: nowrap;
        font-variant-numeric: tabular-nums;
      }
      .empty-hint {
        font-size: 12px;
        color: var(--cb-text-secondary);
        text-align: center;
        margin-top: var(--cb-gap-sm);
      }
    `,
  ];

  private _xToMinutes(clientX: number, rect: DOMRect): number {
    if (rect.width === 0) return 0;
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    return snapMinutes(ratio * 24 * 60, SNAP_MINUTES);
  }

  private _onTrackTap = (event: PointerEvent) => {
    if ((event.target as HTMLElement).classList.contains('point')) return;
    const track = this.shadowRoot?.querySelector('.track');
    if (!track) return;
    const rect = track.getBoundingClientRect();
    const minutes = this._xToMinutes(event.clientX, rect);
    // Avoid creating a transition right on top of an existing one.
    for (const t of this.transitions) {
      const existing = timeStringToMinutes(t.at);
      const distancePct = Math.abs(minutesToPct(existing) - minutesToPct(minutes));
      if (distancePct < TAP_HIT_RADIUS_PCT) return;
    }
    this.dispatchEvent(
      new CustomEvent('transition-add', {
        detail: { at: minutesToTimeString(minutes) },
        bubbles: true,
        composed: true,
      }),
    );
  };

  private _onPointTap = (transition: Transition) => {
    this.dispatchEvent(
      new CustomEvent('transition-edit', {
        detail: { transition },
        bubbles: true,
        composed: true,
      }),
    );
  };

  private _onPointPointerDown = (event: PointerEvent, transition: Transition) => {
    event.stopPropagation();
    if (this._longPressTimer !== null) {
      window.clearTimeout(this._longPressTimer);
    }
    const target = event.currentTarget as HTMLElement;
    let longPressFired = false;
    this._longPressTimer = window.setTimeout(() => {
      longPressFired = true;
      this._longPressTimer = null;
      this.dispatchEvent(
        new CustomEvent('transition-delete', {
          detail: { at: transition.at },
          bubbles: true,
          composed: true,
        }),
      );
    }, LONG_PRESS_MS);
    const onUp = () => {
      target.removeEventListener('pointerup', onUp);
      target.removeEventListener('pointercancel', onUp);
      target.removeEventListener('pointerleave', onUp);
      if (this._longPressTimer !== null) {
        window.clearTimeout(this._longPressTimer);
        this._longPressTimer = null;
        if (!longPressFired) this._onPointTap(transition);
      }
    };
    target.addEventListener('pointerup', onUp);
    target.addEventListener('pointercancel', onUp);
    target.addEventListener('pointerleave', onUp);
  };

  private _onPointKeyDown = (event: KeyboardEvent, transition: Transition) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      this._onPointTap(transition);
    } else if (event.key === 'Delete' || event.key === 'Backspace') {
      event.preventDefault();
      this.dispatchEvent(
        new CustomEvent('transition-delete', {
          detail: { at: transition.at },
          bubbles: true,
          composed: true,
        }),
      );
    }
  };

  protected override render() {
    return html`
      <div class="ruler">
        <div class="track" @click=${this._onTrackTap}></div>
        ${HOUR_LABELS.map((h, i) => {
          const cls =
            i === 0
              ? 'hour-tick start'
              : i === HOUR_LABELS.length - 1
                ? 'hour-tick terminal'
                : 'hour-tick';
          return html`<div class=${cls} style="left:${(h / 24) * 100}%">${h}h</div>`;
        })}
        ${this.transitions.map((t) => {
          const mins = timeStringToMinutes(t.at);
          const pct = minutesToPct(mins);
          const aria = `Transition at ${t.at}, low ${t.low.toFixed(1)} degrees, high ${t.high.toFixed(1)} degrees. Tap to edit, long-press or Delete to remove.`;
          return html`
            <button
              class="point"
              role="button"
              style="left:${pct}%"
              tabindex="0"
              aria-label=${aria}
              data-at=${t.at}
              @pointerdown=${(e: PointerEvent) => this._onPointPointerDown(e, t)}
              @keydown=${(e: KeyboardEvent) => this._onPointKeyDown(e, t)}
            ></button>
            <div class="point-label" style="left:${pct}%">
              ${t.at} · ${t.low.toFixed(1)}–${t.high.toFixed(1)}°
            </div>
          `;
        })}
      </div>
      ${this.transitions.length === 0
        ? html`<div class="empty-hint">Tap the timeline to add a transition.</div>`
        : null}
    `;
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'timeline-editor': TimelineEditor;
  }
}
