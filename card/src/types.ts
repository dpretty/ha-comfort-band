/**
 * TypeScript types for the Comfort Band card.
 *
 * Two groups of types:
 *   1. Minimal Home Assistant frontend shapes — only what the card uses.
 *   2. Comfort Band domain types mirroring `storage.py` and `services.yaml`.
 */

// ---------- Home Assistant ----------

export interface HassEntityAttributes {
  friendly_name?: string;
  unit_of_measurement?: string;
  device_class?: string;
  options?: string[];
  [key: string]: unknown;
}

export interface HassEntity {
  entity_id: string;
  state: string;
  attributes: HassEntityAttributes;
  last_changed: string;
  last_updated: string;
}

export interface EntityRegistryEntry {
  entity_id: string;
  unique_id: string;
  platform: string;
  device_id: string | null;
  disabled_by: string | null;
  hidden_by: string | null;
  name: string | null;
  area_id: string | null;
  translation_key: string | null;
}

export interface DeviceRegistryEntry {
  id: string;
  identifiers: Array<[string, string]>;
  name: string | null;
  name_by_user: string | null;
  area_id: string | null;
}

export interface HomeAssistant {
  states: Record<string, HassEntity>;
  entities: Record<string, EntityRegistryEntry>;
  devices: Record<string, DeviceRegistryEntry>;
  callService(
    domain: string,
    service: string,
    serviceData?: Record<string, unknown>,
  ): Promise<unknown>;
  callWS<T = unknown>(msg: { type: string } & Record<string, unknown>): Promise<T>;
}

// ---------- Comfort Band domain ----------

/** Mirrors `StoredTransition` in storage.py. */
export interface Transition {
  /** "HH:MM" 24-hour clock. */
  at: string;
  low: number;
  high: number;
}

/** Mirrors `StoredProfileSchedule` in storage.py. */
export interface ProfileSchedule {
  baseline: Transition[];
  current: Transition[];
}

/** Mirrors `StoredZone` in storage.py. */
export interface StoredZone {
  zone_name: string;
  schedules: Record<string, ProfileSchedule>;
  manual_low: number;
  manual_high: number;
  override_hours: number;
  override_until: string | null;
  deadband_below: number;
  deadband_above: number;
  min_cycle_minutes: number;
  enabled: boolean;
  last_action_at: string | null;
  last_action: string | null;
}

/** Values of `sensor.{zone}_current_action`. Mirrors const.py ACTION_*. */
export type ComfortBandAction = 'heating' | 'cooling' | 'idle' | 'unknown';

// ---------- Card config ----------

export interface ComfortBandCardConfig {
  type: string;
  zone: string;
  /** Compact mode locks the tile (no expand-on-tap). */
  compact?: boolean;
}
