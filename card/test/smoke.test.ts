import { describe, it, expect } from 'vitest';

describe('comfort-band-card module', () => {
  it('registers comfort-band-card as a custom element on import', async () => {
    await import('../src/index.js');
    expect(customElements.get('comfort-band-card')).toBeDefined();
  });
});
