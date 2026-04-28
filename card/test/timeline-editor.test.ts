import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/timeline-editor.js';
import type { TimelineEditor } from '../src/timeline-editor.js';
import type { Transition } from '../src/types.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

const SCHEDULE: Transition[] = [
  { at: '06:00', low: 20, high: 23 },
  { at: '22:00', low: 18, high: 21 },
];

async function timeline(transitions: Transition[] = SCHEDULE): Promise<TimelineEditor> {
  return mount('timeline-editor', { transitions });
}

describe('timeline-editor', () => {
  it('renders one button per transition with the right time/temp label', async () => {
    const el = await timeline();
    const points = el.shadowRoot!.querySelectorAll('.point');
    expect(points.length).toBe(2);
    const labels = el.shadowRoot!.querySelectorAll('.point-label');
    expect(labels[0].textContent).toContain('06:00');
    expect(labels[0].textContent).toContain('20.0');
    expect(labels[1].textContent).toContain('22:00');
  });

  it('shows hour ticks at 0/6/12/18/24', async () => {
    const el = await timeline();
    const ticks = el.shadowRoot!.querySelectorAll('.hour-tick');
    expect(ticks.length).toBe(5);
    expect(Array.from(ticks).map((t) => t.textContent!.trim())).toEqual([
      '0h',
      '6h',
      '12h',
      '18h',
      '24h',
    ]);
  });

  it('emits transition-edit when a point is tapped (Enter key)', async () => {
    const el = await timeline();
    const events: Transition[] = [];
    el.addEventListener('transition-edit', (e) =>
      events.push((e as CustomEvent).detail.transition),
    );
    const point = el.shadowRoot!.querySelector('.point') as HTMLElement;
    point.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect(events).toEqual([SCHEDULE[0]]);
  });

  it('emits transition-delete on Delete keypress', async () => {
    const el = await timeline();
    const fire = vi.fn();
    el.addEventListener('transition-delete', fire);
    const points = el.shadowRoot!.querySelectorAll('.point');
    (points[1] as HTMLElement).dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Delete', bubbles: true }),
    );
    expect(fire).toHaveBeenCalledOnce();
    expect(fire.mock.calls[0][0].detail).toEqual({ at: '22:00' });
  });

  it('shows the empty hint when there are no transitions', async () => {
    const el = await timeline([]);
    expect(el.shadowRoot!.querySelector('.empty-hint')).not.toBeNull();
  });

  it('does not emit add when the empty timeline tap is too close to an existing point', async () => {
    const el = await timeline();
    const fire = vi.fn();
    el.addEventListener('transition-add', fire);

    // Build a synthetic click on the track at exactly 06:00 — which a real
    // user would map to existing 06:00 transition (the .point handles it).
    // The track ignores taps if a point exists within ~1.5%.
    const track = el.shadowRoot!.querySelector('.track') as HTMLElement;
    Object.defineProperty(track, 'getBoundingClientRect', {
      value: () => ({
        left: 0,
        width: 480,
        right: 480,
        top: 0,
        bottom: 24,
        height: 24,
        x: 0,
        y: 0,
        toJSON() {},
      }),
      configurable: true,
    });
    // 06:00 is at 6/24 = 25% of width = 120px.
    track.dispatchEvent(new MouseEvent('click', { clientX: 120, bubbles: true }));
    expect(fire).not.toHaveBeenCalled();
  });

  it('emits transition-add with snapped HH:MM when the timeline is tapped at empty space', async () => {
    const el = await timeline();
    const fire = vi.fn();
    el.addEventListener('transition-add', fire);

    const track = el.shadowRoot!.querySelector('.track') as HTMLElement;
    Object.defineProperty(track, 'getBoundingClientRect', {
      value: () => ({
        left: 0,
        width: 480,
        right: 480,
        top: 0,
        bottom: 24,
        height: 24,
        x: 0,
        y: 0,
        toJSON() {},
      }),
      configurable: true,
    });
    // 12:00 = 50% = 240px.
    track.dispatchEvent(new MouseEvent('click', { clientX: 240, bubbles: true }));
    expect(fire).toHaveBeenCalledOnce();
    expect(fire.mock.calls[0][0].detail).toEqual({ at: '12:00' });
  });
});
