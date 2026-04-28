import { describe, it, expect } from 'vitest';
import { findActiveProfileEntity, findZoneEntities } from '../src/helpers.js';
import type { DeviceRegistryEntry, EntityRegistryEntry, HomeAssistant } from '../src/types.js';

function makeEntity(entity_id: string, unique_id: string, device_id: string): EntityRegistryEntry {
  return {
    entity_id,
    unique_id,
    platform: 'comfort_band',
    device_id,
    disabled_by: null,
    hidden_by: null,
    name: null,
    area_id: null,
    translation_key: null,
  };
}

function makeDevice(
  id: string,
  identifiers: Array<[string, string]>,
  name: string | null,
  name_by_user: string | null = null,
): DeviceRegistryEntry {
  return { id, identifiers, name, name_by_user, area_id: null };
}

function makeHass(devices: DeviceRegistryEntry[], entities: EntityRegistryEntry[]): HomeAssistant {
  return {
    states: {},
    devices: Object.fromEntries(devices.map((d) => [d.id, d])),
    entities: Object.fromEntries(entities.map((e) => [e.entity_id, e])),
    callService: () => Promise.resolve(),
    callWS: () => Promise.resolve() as Promise<never>,
  };
}

const ALL_KEYS = [
  'effective_low',
  'effective_high',
  'room_temperature',
  'override_ends',
  'current_action',
  'override_active',
  'manual_low',
  'manual_high',
  'override_hours',
  'deadband_below',
  'deadband_above',
  'min_cycle_minutes',
  'cancel_override',
  'enabled',
];

describe('findZoneEntities', () => {
  it('resolves every entity for a zone with the canonical entity-id pattern', () => {
    const device = makeDevice('dev-gym', [['comfort_band', 'zone:gym']], 'Gym');
    const entities = ALL_KEYS.map((key) =>
      makeEntity(`sensor.gym_${key}`, `gym_${key}`, 'dev-gym'),
    );
    const hass = makeHass([device], entities);

    const out = findZoneEntities(hass, 'gym');

    expect(out.deviceId).toBe('dev-gym');
    expect(out.deviceName).toBe('Gym');
    expect(out.effectiveLow).toBe('sensor.gym_effective_low');
    expect(out.currentAction).toBe('sensor.gym_current_action');
    expect(out.manualLow).toBe('sensor.gym_manual_low');
    expect(out.cancelOverride).toBe('sensor.gym_cancel_override');
    expect(out.enabled).toBe('sensor.gym_enabled');
  });

  it('resolves entities even when entity_ids have collision suffixes (`_2`)', () => {
    // Reproduces the post-cutover state: unique_id is canonical (`main_*`)
    // but entity_id has a `_2` suffix because the legacy entity squatted the slug.
    const device = makeDevice('dev-main', [['comfort_band', 'zone:main']], 'Main');
    const entities = [
      makeEntity('sensor.main_room_temperature_2', 'main_room_temperature', 'dev-main'),
      makeEntity('sensor.main_current_action_2', 'main_current_action', 'dev-main'),
      makeEntity('number.main_manual_low_2', 'main_manual_low', 'dev-main'),
    ];
    const hass = makeHass([device], entities);

    const out = findZoneEntities(hass, 'main');

    expect(out.roomTemperature).toBe('sensor.main_room_temperature_2');
    expect(out.currentAction).toBe('sensor.main_current_action_2');
    expect(out.manualLow).toBe('number.main_manual_low_2');
  });

  it('resolves entities when the entity_id is wholly renamed (e.g. `gym_hvac_new_*`)', () => {
    // Reproduces the gym zone's renamed entity_ids — slugs differ from the
    // canonical pattern but unique_ids stay stable. Lookup by device + unique_id
    // sidesteps the entity-id naming chaos.
    const device = makeDevice('dev-gym', [['comfort_band', 'zone:gym']], 'Gym');
    const entities = [
      makeEntity('sensor.gym_hvac_new_room_temperature', 'gym_room_temperature', 'dev-gym'),
      makeEntity('binary_sensor.gym_hvac_new_override_active', 'gym_override_active', 'dev-gym'),
    ];
    const hass = makeHass([device], entities);

    const out = findZoneEntities(hass, 'gym');

    expect(out.roomTemperature).toBe('sensor.gym_hvac_new_room_temperature');
    expect(out.overrideActive).toBe('binary_sensor.gym_hvac_new_override_active');
  });

  it('prefers `name_by_user` over `name` when present', () => {
    const device = makeDevice('dev-mbr', [['comfort_band', 'zone:mbr']], 'Mbr', 'Master Bedroom');
    const hass = makeHass([device], []);

    expect(findZoneEntities(hass, 'mbr').deviceName).toBe('Master Bedroom');
  });

  it('returns null fields when the zone device is not registered', () => {
    const hass = makeHass([], []);

    const out = findZoneEntities(hass, 'office');

    expect(out.deviceId).toBe(null);
    expect(out.effectiveLow).toBe(null);
    expect(out.currentAction).toBe(null);
  });

  it('ignores entities from other platforms or other devices', () => {
    const device = makeDevice('dev-gym', [['comfort_band', 'zone:gym']], 'Gym');
    const entities: EntityRegistryEntry[] = [
      makeEntity('sensor.gym_room_temperature', 'gym_room_temperature', 'dev-gym'),
      // Foreign platform — same unique_id pattern but different platform.
      {
        ...makeEntity('sensor.fake_gym_current_action', 'gym_current_action', 'dev-gym'),
        platform: 'template',
      },
      // Another device — sibling device that shouldn't leak through.
      makeEntity('sensor.other_room_temperature', 'other_room_temperature', 'dev-other'),
    ];
    const hass = makeHass([device], entities);

    const out = findZoneEntities(hass, 'gym');

    expect(out.roomTemperature).toBe('sensor.gym_room_temperature');
    expect(out.currentAction).toBe(null);
  });

  it('skips entities whose unique_id does not start with the zone prefix', () => {
    // Defensive: an entity tagged to the zone device but with an unexpected
    // unique_id (e.g. a future version's diagnostic) should be ignored.
    const device = makeDevice('dev-gym', [['comfort_band', 'zone:gym']], 'Gym');
    const entities = [
      makeEntity('sensor.gym_room_temperature', 'gym_room_temperature', 'dev-gym'),
      makeEntity('sensor.gym_diag_x', 'diag_x', 'dev-gym'),
    ];
    const hass = makeHass([device], entities);

    const out = findZoneEntities(hass, 'gym');

    expect(out.roomTemperature).toBe('sensor.gym_room_temperature');
    // The unrecognised entity didn't crash — and didn't get assigned anywhere.
    expect(Object.values(out).every((v) => v !== 'sensor.gym_diag_x')).toBe(true);
  });
});

describe('findActiveProfileEntity', () => {
  it('resolves the singleton profile-manager select', () => {
    const device = makeDevice(
      'dev-pm',
      [['comfort_band', 'profile_manager']],
      'Comfort Band Profiles',
    );
    const entities = [
      makeEntity(
        'select.comfort_band_profiles_active_profile',
        'profile_manager_active_profile',
        'dev-pm',
      ),
    ];
    const hass = makeHass([device], entities);

    expect(findActiveProfileEntity(hass)).toBe('select.comfort_band_profiles_active_profile');
  });

  it('returns null when the profile-manager device is missing', () => {
    expect(findActiveProfileEntity(makeHass([], []))).toBe(null);
  });
});
