import { afterEach, describe, it, expect } from 'vitest';
import '../src/band-gauge.js';
import type { BandGauge } from '../src/band-gauge.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

async function gauge(props: Partial<BandGauge>): Promise<BandGauge> {
  return mount('band-gauge', props);
}

function svgContent(el: BandGauge): string {
  return el.shadowRoot!.innerHTML;
}

describe('band-gauge', () => {
  it('renders an SVG with track + band + marker when all values are known', async () => {
    const el = await gauge({ low: 19, high: 23, room: 21, action: 'idle' });
    const html = svgContent(el);
    expect(html).toContain('<svg');
    expect(html).toContain('class="track"');
    expect(html).toContain('class="band"');
    expect(html).toContain('<circle');
  });

  it('omits the band stripe when low/high are missing', async () => {
    const el = await gauge({ low: NaN, high: NaN, room: 21, action: 'unknown' });
    const html = svgContent(el);
    expect(html).toContain('class="track"');
    expect(html).not.toContain('class="band"');
  });

  it('omits the marker when room temperature is unknown', async () => {
    const el = await gauge({ low: 19, high: 23, room: NaN, action: 'idle' });
    const html = svgContent(el);
    expect(html).toContain('class="band"');
    expect(html).not.toContain('<circle');
  });

  it('writes a descriptive aria-label including the action', async () => {
    const el = await gauge({ low: 19, high: 23, room: 21.4, action: 'heating' });
    const aria = el.shadowRoot!.querySelector('svg')!.getAttribute('aria-label');
    expect(aria).toContain('19.0');
    expect(aria).toContain('21.4');
    expect(aria).toContain('23.0');
    expect(aria).toContain('heating');
  });

  it('uses the heating colour variable when action is heating', async () => {
    const el = await gauge({ low: 19, high: 23, room: 17, action: 'heating' });
    expect(svgContent(el)).toContain('--cb-action-heating');
  });

  it('uses the cooling colour variable when action is cooling', async () => {
    const el = await gauge({ low: 19, high: 23, room: 25, action: 'cooling' });
    expect(svgContent(el)).toContain('--cb-action-cooling');
  });

  it('falls back to the unknown colour for unrecognised action strings', async () => {
    const el = await gauge({ low: 19, high: 23, room: 21, action: 'bogus' });
    expect(svgContent(el)).toContain('--cb-action-unknown');
  });
});
