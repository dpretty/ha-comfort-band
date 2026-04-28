import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/card-editor.js';
import type { ComfortBandCardEditor } from '../src/card-editor.js';
import type { HomeAssistant } from '../src/types.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

function makeHass(zones: string[]): HomeAssistant {
  return {
    states: {},
    entities: {},
    devices: Object.fromEntries(
      zones.map((slug, i) => [
        `dev-${i}`,
        {
          id: `dev-${i}`,
          identifiers: [['comfort_band', `zone:${slug}`]] as Array<[string, string]>,
          name: slug,
          name_by_user: null,
          area_id: null,
        },
      ]),
    ),
    callService: vi.fn(),
    callWS: vi.fn(),
  };
}

async function editor(
  hass: HomeAssistant,
  config: Parameters<ComfortBandCardEditor['setConfig']>[0] = {
    type: 'custom:comfort-band-card',
    zone: '',
  },
): Promise<ComfortBandCardEditor> {
  const el = await mount('comfort-band-card-editor', { hass });
  el.setConfig(config);
  await el.updateComplete;
  return el;
}

describe('comfort-band-card-editor', () => {
  it('lists every Comfort Band zone in the dropdown, sorted', async () => {
    const el = await editor(makeHass(['mbr', 'gym', 'office']));
    const options = el.shadowRoot!.querySelectorAll('option:not([disabled])');
    const values = Array.from(options).map((o) => (o as HTMLOptionElement).value);
    expect(values).toEqual(['gym', 'mbr', 'office']);
  });

  it('shows a friendly empty state when no zones are registered', async () => {
    const el = await editor(makeHass([]));
    expect(el.shadowRoot!.querySelector('.empty')!.textContent).toContain('No Comfort Band zones');
  });

  it('fires config-changed with the new zone when the dropdown changes', async () => {
    const el = await editor(makeHass(['gym', 'office']));
    const fire = vi.fn();
    el.addEventListener('config-changed', fire);
    const select = el.shadowRoot!.querySelector('select') as HTMLSelectElement;
    select.value = 'office';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    expect(fire.mock.calls[0][0].detail.config).toEqual({
      type: 'custom:comfort-band-card',
      zone: 'office',
    });
  });

  it('toggles `compact: true` from the checkbox', async () => {
    const el = await editor(makeHass(['gym']), {
      type: 'custom:comfort-band-card',
      zone: 'gym',
    });
    const fire = vi.fn();
    el.addEventListener('config-changed', fire);
    const checkbox = el.shadowRoot!.querySelector('input[type="checkbox"]') as HTMLInputElement;
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
    expect(fire.mock.calls[0][0].detail.config).toEqual({
      type: 'custom:comfort-band-card',
      zone: 'gym',
      compact: true,
    });
  });

  it('removes `compact` from the config when unchecked', async () => {
    const el = await editor(makeHass(['gym']), {
      type: 'custom:comfort-band-card',
      zone: 'gym',
      compact: true,
    });
    const fire = vi.fn();
    el.addEventListener('config-changed', fire);
    const checkbox = el.shadowRoot!.querySelector('input[type="checkbox"]') as HTMLInputElement;
    checkbox.checked = false;
    checkbox.dispatchEvent(new Event('change', { bubbles: true }));
    expect(fire.mock.calls[0][0].detail.config).toEqual({
      type: 'custom:comfort-band-card',
      zone: 'gym',
    });
  });
});
