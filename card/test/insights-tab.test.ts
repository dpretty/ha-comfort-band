import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/tabs/insights-tab.js';
import type { ComfortBandInsightsTab } from '../src/tabs/insights-tab.js';
import type { ZoneEntities } from '../src/helpers.js';
import type { HomeAssistant } from '../src/types.js';
import { mount, teardown } from './_fixture.js';

afterEach(() => {
  teardown();
  delete (window as { loadCardHelpers?: unknown }).loadCardHelpers;
});

function makeEntities(
  roomTemperature: string | null = 'sensor.gym_room_temperature',
): ZoneEntities {
  return {
    effectiveLow: null,
    effectiveHigh: null,
    roomTemperature,
    overrideEnds: null,
    currentAction: null,
    overrideActive: null,
    manualLow: null,
    manualHigh: null,
    overrideHours: null,
    deadbandBelow: null,
    deadbandAbove: null,
    minCycleMinutes: null,
    cancelOverride: null,
    enabled: null,
    deviceId: 'dev-gym',
    deviceName: 'Gym',
  };
}

function makeHass(): HomeAssistant {
  return {
    states: {},
    devices: {},
    entities: {},
    callService: vi.fn(),
    callWS: vi.fn(),
  };
}

async function tab(props: Partial<ComfortBandInsightsTab>): Promise<ComfortBandInsightsTab> {
  return mount('comfort-band-insights-tab', props);
}

describe('comfort-band-insights-tab', () => {
  it('renders an empty message when no room temperature sensor is registered', async () => {
    const el = await tab({ hass: makeHass(), entities: makeEntities(null) });
    expect(el.shadowRoot!.querySelector('.empty')!.textContent).toContain('No room temperature');
  });

  it('shows the fallback link when loadCardHelpers is not on window', async () => {
    delete (window as { loadCardHelpers?: unknown }).loadCardHelpers;
    const el = await tab({ hass: makeHass(), entities: makeEntities() });
    // firstUpdated runs synchronously here; give the microtask a chance.
    await el.updateComplete;
    const fallback = el.shadowRoot!.querySelector('.fallback');
    expect(fallback).not.toBeNull();
    expect(fallback!.querySelector('a')!.getAttribute('href')).toContain(
      'sensor.gym_room_temperature',
    );
  });

  it('uses loadCardHelpers + createCardElement when available', async () => {
    const fakeCard = document.createElement('div');
    Object.assign(fakeCard, { hass: undefined as unknown });
    const createCardElement = vi.fn().mockReturnValue(fakeCard);
    (window as { loadCardHelpers?: unknown }).loadCardHelpers = vi
      .fn()
      .mockResolvedValue({ createCardElement });

    const hass = makeHass();
    const el = await tab({ hass, entities: makeEntities() });
    // Wait for the async firstUpdated → loadCardHelpers chain.
    await el.updateComplete;
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;

    expect(createCardElement).toHaveBeenCalledWith({
      type: 'history-graph',
      entities: ['sensor.gym_room_temperature'],
      hours_to_show: 24,
    });
    expect(el.shadowRoot!.querySelector('.graph-container')!.contains(fakeCard)).toBe(true);
    expect((fakeCard as unknown as { hass: unknown }).hass).toBe(hass);
  });

  it('falls back if loadCardHelpers throws', async () => {
    (window as { loadCardHelpers?: unknown }).loadCardHelpers = vi
      .fn()
      .mockRejectedValue(new Error('boom'));

    const el = await tab({ hass: makeHass(), entities: makeEntities() });
    await el.updateComplete;
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;

    expect(el.shadowRoot!.querySelector('.fallback')).not.toBeNull();
  });
});
