import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/tabs/now-tab.js';
import type { ComfortBandNowTab } from '../src/tabs/now-tab.js';
import type { ZoneEntities } from '../src/helpers.js';
import type { HomeAssistant } from '../src/types.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

function makeEntities(zone = 'gym'): ZoneEntities {
  return {
    effectiveLow: `sensor.${zone}_effective_low`,
    effectiveHigh: `sensor.${zone}_effective_high`,
    roomTemperature: `sensor.${zone}_room_temperature`,
    overrideEnds: `sensor.${zone}_override_ends`,
    currentAction: `sensor.${zone}_current_action`,
    overrideActive: `binary_sensor.${zone}_override_active`,
    manualLow: `number.${zone}_manual_low`,
    manualHigh: `number.${zone}_manual_high`,
    overrideHours: `number.${zone}_override_hours`,
    deadbandBelow: `number.${zone}_deadband_below`,
    deadbandAbove: `number.${zone}_deadband_above`,
    minCycleMinutes: `number.${zone}_min_cycle_minutes`,
    cancelOverride: `button.${zone}_cancel_override`,
    enabled: `switch.${zone}_enabled`,
    deviceId: `dev-${zone}`,
    deviceName: zone,
  };
}

function makeHass(overrides: Partial<Record<string, string>> = {}): HomeAssistant {
  const states: Record<string, string> = {
    'sensor.gym_effective_low': '19.5',
    'sensor.gym_effective_high': '22.5',
    'sensor.gym_room_temperature': '21.0',
    'sensor.gym_current_action': 'idle',
    'sensor.gym_override_ends': 'unknown',
    'binary_sensor.gym_override_active': 'off',
    'number.gym_manual_low': '19',
    'number.gym_manual_high': '22',
    'number.gym_override_hours': '3',
    ...overrides,
  };
  return {
    states: Object.fromEntries(
      Object.entries(states).map(([k, v]) => [
        k,
        {
          entity_id: k,
          state: v as string,
          attributes: {},
          last_changed: '',
          last_updated: '',
        },
      ]),
    ),
    devices: {},
    entities: {},
    callService: vi.fn().mockResolvedValue(undefined),
    callWS: vi.fn().mockResolvedValue(undefined),
  };
}

async function nowTab(
  hass: HomeAssistant,
  entities = makeEntities(),
  zone = 'gym',
): Promise<ComfortBandNowTab> {
  return mount('comfort-band-now-tab', { hass, entities, zone });
}

describe('comfort-band-now-tab', () => {
  it('renders prominent room temperature with one decimal', async () => {
    const el = await nowTab(makeHass());
    expect(el.shadowRoot!.querySelector('.room-temp')!.textContent).toContain('21.0°');
  });

  it('omits the action chip when idle', async () => {
    const el = await nowTab(makeHass({ 'sensor.gym_current_action': 'idle' }));
    expect(el.shadowRoot!.querySelector('.action-chip')).toBeNull();
  });

  it('shows the action chip when heating', async () => {
    const el = await nowTab(makeHass({ 'sensor.gym_current_action': 'heating' }));
    expect(el.shadowRoot!.querySelector('.action-chip')!.textContent).toContain('Heating');
  });

  it('initialises slider from manual_low / manual_high entity states', async () => {
    const el = await nowTab(
      makeHass({ 'number.gym_manual_low': '20', 'number.gym_manual_high': '23' }),
    );
    const slider = el.shadowRoot!.querySelector('dual-handle-slider') as HTMLElement & {
      low: number;
      high: number;
    };
    expect(slider.low).toBe(20);
    expect(slider.high).toBe(23);
  });

  it('slider change calls comfort_band.start_override with the new band', async () => {
    const hass = makeHass();
    const el = await nowTab(hass);
    const slider = el.shadowRoot!.querySelector('dual-handle-slider')!;
    slider.dispatchEvent(
      new CustomEvent('change', { detail: { low: 20.5, high: 23.5 }, bubbles: true }),
    );
    await el.updateComplete;
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'start_override', {
      zone: 'gym',
      low: 20.5,
      high: 23.5,
    });
  });

  it('hides the override section when override is inactive', async () => {
    const el = await nowTab(makeHass());
    expect(el.shadowRoot!.textContent).not.toContain('Cancel');
  });

  it('shows the override section + Cancel button when override is active', async () => {
    const el = await nowTab(
      makeHass({
        'binary_sensor.gym_override_active': 'on',
        'sensor.gym_override_ends': new Date(Date.now() + 60 * 60_000).toISOString(),
      }),
    );
    const cancelBtn = el.shadowRoot!.querySelector('.button.secondary');
    expect(cancelBtn).not.toBeNull();
    expect(cancelBtn!.textContent).toContain('Cancel');
  });

  it('Cancel button calls comfort_band.cancel_override', async () => {
    const hass = makeHass({
      'binary_sensor.gym_override_active': 'on',
      'sensor.gym_override_ends': new Date(Date.now() + 60 * 60_000).toISOString(),
    });
    const el = await nowTab(hass);
    const cancel = el.shadowRoot!.querySelector('.button.secondary') as HTMLButtonElement;
    cancel.click();
    await el.updateComplete;
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'cancel_override', {
      zone: 'gym',
    });
  });

  it('clicking a duration preset calls number.set_value on the hours entity', async () => {
    const hass = makeHass({ 'number.gym_override_hours': '3' });
    const el = await nowTab(hass);
    const presets = el.shadowRoot!.querySelectorAll<HTMLButtonElement>('.preset');
    presets[2].click(); // 6h
    await el.updateComplete;
    expect(hass.callService).toHaveBeenCalledWith('number', 'set_value', {
      entity_id: 'number.gym_override_hours',
      value: 6,
    });
  });

  it('marks the active duration preset', async () => {
    const el = await nowTab(makeHass({ 'number.gym_override_hours': '3' }));
    const presets = el.shadowRoot!.querySelectorAll<HTMLButtonElement>('.preset');
    expect(presets[0].classList.contains('active')).toBe(false);
    expect(presets[1].classList.contains('active')).toBe(true);
    expect(presets[2].classList.contains('active')).toBe(false);
  });
});
