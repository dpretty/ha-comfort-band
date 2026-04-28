import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/tabs/profiles-tab.js';
import type { ComfortBandProfilesTab } from '../src/tabs/profiles-tab.js';
import type { HomeAssistant } from '../src/types.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

function makeHass(opts: {
  options?: string[];
  active?: string;
  selectEntity?: string | null;
}): HomeAssistant {
  const entityId = opts.selectEntity ?? 'select.comfort_band_profiles_active_profile';
  const states: Record<string, ReturnType<typeof entityState>> = {};
  const entities: Record<string, unknown> = {};
  const devices: Record<string, unknown> = {};

  if (opts.selectEntity !== null) {
    devices['dev-pm'] = {
      id: 'dev-pm',
      identifiers: [['comfort_band', 'profile_manager']],
      name: 'Comfort Band Profiles',
      name_by_user: null,
      area_id: null,
    };
    entities[entityId] = {
      entity_id: entityId,
      unique_id: 'profile_manager_active_profile',
      platform: 'comfort_band',
      device_id: 'dev-pm',
      disabled_by: null,
      hidden_by: null,
      name: null,
      area_id: null,
      translation_key: 'active_profile',
    };
    states[entityId] = entityState(entityId, opts.active ?? 'home', {
      options: opts.options ?? ['home', 'away', 'sleep'],
    });
  }

  return {
    states: states as HomeAssistant['states'],
    entities: entities as HomeAssistant['entities'],
    devices: devices as HomeAssistant['devices'],
    callService: vi.fn().mockResolvedValue(undefined),
    callWS: vi.fn().mockResolvedValue(undefined),
  };
}

function entityState(entityId: string, state: string, attributes: Record<string, unknown> = {}) {
  return {
    entity_id: entityId,
    state,
    attributes,
    last_changed: '',
    last_updated: '',
  };
}

async function profilesTab(hass: HomeAssistant): Promise<ComfortBandProfilesTab> {
  return mount('comfort-band-profiles-tab', { hass });
}

describe('comfort-band-profiles-tab', () => {
  it('lists every profile from the select options', async () => {
    const el = await profilesTab(makeHass({ options: ['home', 'away', 'sleep'] }));
    const items = el.shadowRoot!.querySelectorAll('li');
    expect(items.length).toBe(3);
    expect(Array.from(items).map((li) => li.querySelector('.name')!.textContent!.trim())).toEqual([
      'home',
      'away',
      'sleep',
    ]);
  });

  it('marks the active profile with the active class and a badge', async () => {
    const el = await profilesTab(makeHass({ options: ['home', 'away', 'sleep'], active: 'away' }));
    const items = el.shadowRoot!.querySelectorAll('li');
    expect(items[0].classList.contains('active')).toBe(false);
    expect(items[1].classList.contains('active')).toBe(true);
    expect(items[1].querySelector('.badge')).not.toBeNull();
    expect(items[2].classList.contains('active')).toBe(false);
  });

  it('clicking a profile fires comfort_band.set_profile', async () => {
    const hass = makeHass({ options: ['home', 'away', 'sleep'], active: 'home' });
    const el = await profilesTab(hass);
    const items = el.shadowRoot!.querySelectorAll<HTMLLIElement>('li');
    items[1].click();
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'set_profile', {
      profile: 'away',
    });
  });

  it('Enter / Space on a focused option selects it (keyboard a11y)', async () => {
    const hass = makeHass({ options: ['home', 'away', 'sleep'], active: 'home' });
    const el = await profilesTab(hass);
    const items = el.shadowRoot!.querySelectorAll<HTMLLIElement>('li');
    items[2].dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'set_profile', {
      profile: 'sleep',
    });
  });

  it('renders a friendly empty state when the profile manager is not yet registered', async () => {
    const el = await profilesTab(makeHass({ selectEntity: null }));
    expect(el.shadowRoot!.textContent).toContain('not registered');
  });

  it('renders a no-profiles message when options is empty', async () => {
    const el = await profilesTab(makeHass({ options: [] }));
    expect(el.shadowRoot!.textContent).toContain('No profiles');
  });
});
