import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/tile.js';
import type { ComfortBandTile } from '../src/tile.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

async function tile(props: Partial<ComfortBandTile>): Promise<ComfortBandTile> {
  return mount('comfort-band-tile', props);
}

describe('comfort-band-tile', () => {
  it('renders zone name and room temperature in degrees with one decimal', async () => {
    const el = await tile({ zoneName: 'Gym', roomTemp: 21.4, low: 19, high: 23, action: 'idle' });
    const sr = el.shadowRoot!;
    expect(sr.querySelector('.zone-name')!.textContent).toContain('Gym');
    expect(sr.querySelector('.room-temp')!.textContent).toContain('21.4°');
  });

  it('shows an em-dash for unknown room temperature', async () => {
    const el = await tile({ zoneName: 'Gym', roomTemp: NaN, action: 'unknown' });
    expect(el.shadowRoot!.querySelector('.room-temp')!.textContent).toContain('—');
  });

  it('renders an action chip when heating or cooling', async () => {
    const heat = await tile({ zoneName: 'Gym', action: 'heating' });
    expect(heat.shadowRoot!.querySelector('.action-chip')?.textContent).toContain('Heating');
    teardown();
    const cool = await tile({ zoneName: 'Gym', action: 'cooling' });
    expect(cool.shadowRoot!.querySelector('.action-chip')?.textContent).toContain('Cooling');
  });

  it('omits the action chip when idle or unknown', async () => {
    const idle = await tile({ zoneName: 'Gym', action: 'idle' });
    expect(idle.shadowRoot!.querySelector('.action-chip')).toBeNull();
    teardown();
    const unk = await tile({ zoneName: 'Gym', action: 'unknown' });
    expect(unk.shadowRoot!.querySelector('.action-chip')).toBeNull();
  });

  it('renders an override pill when override is active', async () => {
    const future = new Date(Date.now() + 90 * 60_000).toISOString(); // +1h 30m
    const el = await tile({
      zoneName: 'Gym',
      action: 'heating',
      overrideActive: true,
      overrideEnds: future,
    });
    const pill = el.shadowRoot!.querySelector('.override-pill');
    expect(pill).not.toBeNull();
    expect(pill!.textContent).toContain('Override');
    expect(pill!.textContent).toContain('1h 30m left');
  });

  it('omits the override pill when override is inactive', async () => {
    const el = await tile({ zoneName: 'Gym', action: 'idle', overrideActive: false });
    expect(el.shadowRoot!.querySelector('.override-pill')).toBeNull();
  });

  it('fires a comfort-band-tile-tap event when tapped', async () => {
    const el = await tile({ zoneName: 'Gym', action: 'idle' });
    const listener = vi.fn();
    el.addEventListener('comfort-band-tile-tap', listener);
    (el.shadowRoot!.querySelector('.tile') as HTMLElement).click();
    expect(listener).toHaveBeenCalledOnce();
  });

  it('does not fire a tap event in noExpand mode', async () => {
    const el = await tile({ zoneName: 'Gym', action: 'idle', noExpand: true });
    const listener = vi.fn();
    el.addEventListener('comfort-band-tile-tap', listener);
    (el.shadowRoot!.querySelector('.tile') as HTMLElement).click();
    expect(listener).not.toHaveBeenCalled();
  });

  it('renders a band-gauge child with the same low/high/room/action', async () => {
    const el = await tile({
      zoneName: 'Gym',
      roomTemp: 21,
      low: 19,
      high: 23,
      action: 'cooling',
    });
    const gauge = el.shadowRoot!.querySelector('band-gauge') as HTMLElement & {
      low: number;
      high: number;
      room: number;
      action: string;
    };
    expect(gauge).not.toBeNull();
    expect(gauge.low).toBe(19);
    expect(gauge.high).toBe(23);
    expect(gauge.room).toBe(21);
    expect(gauge.action).toBe('cooling');
  });
});
