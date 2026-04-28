import { describe, it, expect, vi } from 'vitest';
import {
  addTransition,
  cancelOverride,
  removeTransition,
  setProfile,
  setSchedule,
  startOverride,
  updateTransition,
} from '../src/services.js';
import type { HomeAssistant } from '../src/types.js';

function makeMockHass() {
  const callService = vi.fn().mockResolvedValue(undefined);
  const hass: HomeAssistant = {
    states: {},
    entities: {},
    devices: {},
    callService,
    callWS: vi.fn().mockResolvedValue(undefined),
  };
  return { hass, callService };
}

describe('service wrappers', () => {
  it('setSchedule passes zone, profile, and transitions verbatim', async () => {
    const { hass, callService } = makeMockHass();
    const transitions = [
      { at: '06:00', low: 20, high: 23 },
      { at: '22:00', low: 18, high: 21 },
    ];

    await setSchedule(hass, { zone: 'gym', profile: 'home', transitions });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'set_schedule', {
      zone: 'gym',
      profile: 'home',
      transitions,
    });
  });

  it('addTransition sends the four required fields', async () => {
    const { hass, callService } = makeMockHass();

    await addTransition(hass, {
      zone: 'gym',
      profile: 'home',
      at: '07:30',
      low: 19.5,
      high: 22.5,
    });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'add_transition', {
      zone: 'gym',
      profile: 'home',
      at: '07:30',
      low: 19.5,
      high: 22.5,
    });
  });

  it('updateTransition matches by `at` time', async () => {
    const { hass, callService } = makeMockHass();

    await updateTransition(hass, {
      zone: 'office',
      profile: 'away',
      at: '06:00',
      low: 17,
      high: 24,
    });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'update_transition', {
      zone: 'office',
      profile: 'away',
      at: '06:00',
      low: 17,
      high: 24,
    });
  });

  it('removeTransition sends only zone, profile, at', async () => {
    const { hass, callService } = makeMockHass();

    await removeTransition(hass, { zone: 'gym', profile: 'home', at: '22:00' });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'remove_transition', {
      zone: 'gym',
      profile: 'home',
      at: '22:00',
    });
  });

  it('startOverride omits undefined optional fields', async () => {
    const { hass, callService } = makeMockHass();

    await startOverride(hass, { zone: 'gym' });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'start_override', {
      zone: 'gym',
    });
  });

  it('startOverride forwards low/high/hours when provided', async () => {
    const { hass, callService } = makeMockHass();

    await startOverride(hass, { zone: 'gym', low: 19, high: 22, hours: 4 });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'start_override', {
      zone: 'gym',
      low: 19,
      high: 22,
      hours: 4,
    });
  });

  it('startOverride forwards a partial subset of optionals', async () => {
    const { hass, callService } = makeMockHass();

    await startOverride(hass, { zone: 'gym', high: 22.5 });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'start_override', {
      zone: 'gym',
      high: 22.5,
    });
  });

  it('cancelOverride sends only zone', async () => {
    const { hass, callService } = makeMockHass();

    await cancelOverride(hass, { zone: 'gym' });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'cancel_override', {
      zone: 'gym',
    });
  });

  it('setProfile sends only profile (no zone)', async () => {
    const { hass, callService } = makeMockHass();

    await setProfile(hass, { profile: 'away' });

    expect(callService).toHaveBeenCalledWith('comfort_band', 'set_profile', {
      profile: 'away',
    });
  });
});
