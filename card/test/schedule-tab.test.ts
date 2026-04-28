import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/tabs/schedule-tab.js';
import type { ComfortBandScheduleTab } from '../src/tabs/schedule-tab.js';
import type { HomeAssistant, ProfileSchedule, Transition } from '../src/types.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

interface FakeHassOptions {
  initialSchedule?: ProfileSchedule | null;
  active?: string;
}

function makeHass(opts: FakeHassOptions = {}): HomeAssistant {
  let stored: ProfileSchedule | null = opts.initialSchedule ?? null;
  const callWS = vi.fn(async (msg: { type: string } & Record<string, unknown>) => {
    if (msg.type === 'comfort_band/get_schedule') return stored;
    return null;
  });
  const callService = vi.fn(
    async (_domain: string, service: string, data?: Record<string, unknown>) => {
      if (service === 'set_schedule' && data) {
        stored = {
          baseline: [...((data.transitions as Transition[]) ?? [])],
          current: [...((data.transitions as Transition[]) ?? [])],
        };
      }
    },
  );
  return {
    states: {
      'select.comfort_band_profiles_active_profile': {
        entity_id: 'select.comfort_band_profiles_active_profile',
        state: opts.active ?? 'home',
        attributes: { options: ['home', 'away', 'sleep'] },
        last_changed: '',
        last_updated: '',
      },
    },
    devices: {
      'dev-pm': {
        id: 'dev-pm',
        identifiers: [['comfort_band', 'profile_manager']],
        name: 'Comfort Band Profiles',
        name_by_user: null,
        area_id: null,
      },
    },
    entities: {
      'select.comfort_band_profiles_active_profile': {
        entity_id: 'select.comfort_band_profiles_active_profile',
        unique_id: 'profile_manager_active_profile',
        platform: 'comfort_band',
        device_id: 'dev-pm',
        disabled_by: null,
        hidden_by: null,
        name: null,
        area_id: null,
        translation_key: 'active_profile',
      },
    },
    callService,
    callWS: callWS as HomeAssistant['callWS'],
  };
}

async function tab(hass: HomeAssistant, zone = 'gym'): Promise<ComfortBandScheduleTab> {
  const el = await mount('comfort-band-schedule-tab', { hass, zone });
  // Allow the async fetch in willUpdate to resolve.
  await new Promise((r) => setTimeout(r, 0));
  await el.updateComplete;
  return el;
}

describe('comfort-band-schedule-tab', () => {
  it('fetches the schedule for the active profile via callWS', async () => {
    const hass = makeHass({
      initialSchedule: {
        baseline: [{ at: '06:00', low: 20, high: 23 }],
        current: [{ at: '06:00', low: 20, high: 23 }],
      },
    });
    await tab(hass);
    expect(hass.callWS).toHaveBeenCalledWith({
      type: 'comfort_band/get_schedule',
      zone: 'gym',
      profile: 'home',
    });
  });

  it('renders transitions from the fetched schedule', async () => {
    const hass = makeHass({
      initialSchedule: {
        baseline: [
          { at: '06:00', low: 20, high: 23 },
          { at: '22:00', low: 18, high: 21 },
        ],
        current: [],
      },
    });
    const el = await tab(hass);
    const points = el
      .shadowRoot!.querySelector('timeline-editor')!
      .shadowRoot!.querySelectorAll('.point');
    expect(points.length).toBe(2);
  });

  it('handles a null schedule (zone has no profile schedule yet) without error', async () => {
    const hass = makeHass({ initialSchedule: null });
    const el = await tab(hass);
    expect(el.shadowRoot!.textContent).not.toContain('Failed');
    const editor = el.shadowRoot!.querySelector('timeline-editor');
    expect(editor).not.toBeNull();
  });

  it('persists deletions via comfort_band.set_schedule', async () => {
    const hass = makeHass({
      initialSchedule: {
        baseline: [
          { at: '06:00', low: 20, high: 23 },
          { at: '22:00', low: 18, high: 21 },
        ],
        current: [],
      },
    });
    const el = await tab(hass);
    const editor = el.shadowRoot!.querySelector('timeline-editor')!;
    editor.dispatchEvent(
      new CustomEvent('transition-delete', {
        detail: { at: '22:00' },
        bubbles: true,
        composed: true,
      }),
    );
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'set_schedule', {
      zone: 'gym',
      profile: 'home',
      transitions: [{ at: '06:00', low: 20, high: 23 }],
    });
  });

  it('opens the dialog on transition-edit then persists the new values on save', async () => {
    const hass = makeHass({
      initialSchedule: {
        baseline: [{ at: '06:00', low: 20, high: 23 }],
        current: [],
      },
    });
    const el = await tab(hass);

    const editor = el.shadowRoot!.querySelector('timeline-editor')!;
    editor.dispatchEvent(
      new CustomEvent('transition-edit', {
        detail: { transition: { at: '06:00', low: 20, high: 23 } },
        bubbles: true,
        composed: true,
      }),
    );
    await el.updateComplete;
    const dlg = el.shadowRoot!.querySelector('transition-edit-dialog');
    expect(dlg).not.toBeNull();

    dlg!.dispatchEvent(
      new CustomEvent('dialog-save', {
        detail: { transition: { at: '06:00', low: 20, high: 23.5 } },
        bubbles: true,
        composed: true,
      }),
    );
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'set_schedule', {
      zone: 'gym',
      profile: 'home',
      transitions: [{ at: '06:00', low: 20, high: 23.5 }],
    });
  });

  it('opens the add dialog on transition-add and persists the new transition on save', async () => {
    const hass = makeHass({ initialSchedule: null });
    const el = await tab(hass);

    const editor1 = el.shadowRoot!.querySelector('timeline-editor')!;
    editor1.dispatchEvent(
      new CustomEvent('transition-add', {
        detail: { at: '07:00' },
        bubbles: true,
        composed: true,
      }),
    );
    await el.updateComplete;
    const dlg1 = el.shadowRoot!.querySelector('transition-edit-dialog');
    expect(dlg1).not.toBeNull();

    dlg1!.dispatchEvent(
      new CustomEvent('dialog-save', {
        detail: { transition: { at: '07:00', low: 19, high: 22 } },
        bubbles: true,
        composed: true,
      }),
    );
    await new Promise((r) => setTimeout(r, 0));
    await el.updateComplete;
    expect(hass.callService).toHaveBeenCalledWith('comfort_band', 'set_schedule', {
      zone: 'gym',
      profile: 'home',
      transitions: [{ at: '07:00', low: 19, high: 22 }],
    });
  });

  it('cancel returns to the list mode without writing', async () => {
    const hass = makeHass({ initialSchedule: null });
    const el = await tab(hass);
    const editor2 = el.shadowRoot!.querySelector('timeline-editor')!;
    editor2.dispatchEvent(
      new CustomEvent('transition-add', {
        detail: { at: '08:00' },
        bubbles: true,
        composed: true,
      }),
    );
    await el.updateComplete;
    const dlg2 = el.shadowRoot!.querySelector('transition-edit-dialog')!;
    dlg2.dispatchEvent(new CustomEvent('dialog-cancel', { bubbles: true, composed: true }));
    await el.updateComplete;
    expect(el.shadowRoot!.querySelector('transition-edit-dialog')).toBeNull();
    expect(hass.callService).not.toHaveBeenCalled();
  });
});
