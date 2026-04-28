import { afterEach, describe, it, expect, vi } from 'vitest';
import '../src/transition-edit-dialog.js';
import type { TransitionEditDialog } from '../src/transition-edit-dialog.js';
import { mount, teardown } from './_fixture.js';

afterEach(teardown);

async function dialog(props: Partial<TransitionEditDialog> = {}): Promise<TransitionEditDialog> {
  return mount('transition-edit-dialog', props);
}

describe('transition-edit-dialog', () => {
  it('seeds inputs from the `transition` prop', async () => {
    const el = await dialog({ transition: { at: '08:30', low: 20.5, high: 23.5 } });
    const inputs = el.shadowRoot!.querySelectorAll<HTMLInputElement>('input');
    expect(inputs[0].value).toBe('08:30');
    expect(inputs[1].value).toBe('20.5');
    expect(inputs[2].value).toBe('23.5');
  });

  it('shows "Add transition" header when isNew is true', async () => {
    const el = await dialog({ isNew: true });
    expect(el.shadowRoot!.querySelector('h3')!.textContent).toContain('Add');
  });

  it('shows "Edit transition" + Delete button when isNew is false', async () => {
    const el = await dialog({
      transition: { at: '06:00', low: 19, high: 22 },
      isNew: false,
    });
    expect(el.shadowRoot!.querySelector('h3')!.textContent).toContain('Edit');
    expect(el.shadowRoot!.querySelector('.button.danger')).not.toBeNull();
  });

  it('Save click fires dialog-save with current values when valid', async () => {
    const el = await dialog({ transition: { at: '06:00', low: 19, high: 22 }, isNew: false });
    const fire = vi.fn();
    el.addEventListener('dialog-save', fire);
    (el.shadowRoot!.querySelector('.button.primary') as HTMLButtonElement).click();
    expect(fire).toHaveBeenCalledOnce();
    expect(fire.mock.calls[0][0].detail.transition).toEqual({ at: '06:00', low: 19, high: 22 });
  });

  it('blocks save and surfaces an error when low >= high', async () => {
    const el = await dialog({
      transition: { at: '06:00', low: 22, high: 22 },
      isNew: false,
    });
    const fire = vi.fn();
    el.addEventListener('dialog-save', fire);
    (el.shadowRoot!.querySelector('.button.primary') as HTMLButtonElement).click();
    await el.updateComplete;
    expect(fire).not.toHaveBeenCalled();
    expect(el.shadowRoot!.querySelector('.error')!.textContent).toContain('less than high');
  });

  it('blocks save and surfaces an error for invalid time format', async () => {
    const el = await dialog({ transition: { at: '6:0', low: 19, high: 22 }, isNew: false });
    const fire = vi.fn();
    el.addEventListener('dialog-save', fire);
    (el.shadowRoot!.querySelector('.button.primary') as HTMLButtonElement).click();
    await el.updateComplete;
    expect(fire).not.toHaveBeenCalled();
    expect(el.shadowRoot!.querySelector('.error')!.textContent).toContain('HH:MM');
  });

  it('Cancel click fires dialog-cancel', async () => {
    const el = await dialog({ transition: { at: '06:00', low: 19, high: 22 }, isNew: false });
    const fire = vi.fn();
    el.addEventListener('dialog-cancel', fire);
    (el.shadowRoot!.querySelector('.button.secondary') as HTMLButtonElement).click();
    expect(fire).toHaveBeenCalledOnce();
  });

  it('Delete click fires dialog-delete with the original `at`', async () => {
    const el = await dialog({ transition: { at: '06:00', low: 19, high: 22 }, isNew: false });
    const fire = vi.fn();
    el.addEventListener('dialog-delete', fire);
    (el.shadowRoot!.querySelector('.button.danger') as HTMLButtonElement).click();
    expect(fire.mock.calls[0][0].detail).toEqual({ at: '06:00' });
  });
});
