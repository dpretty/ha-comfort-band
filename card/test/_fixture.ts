/**
 * Minimal Lit element fixture helpers for component tests.
 *
 * happy-dom + Lit don't need anything fancy — `createElement`, append, set
 * properties, await `updateComplete`. This file just dries up the boilerplate.
 */

interface LitLike extends HTMLElement {
  updateComplete: Promise<boolean>;
}

export async function mount<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  props: Partial<HTMLElementTagNameMap[K]> = {},
): Promise<HTMLElementTagNameMap[K]> {
  const el = document.createElement(tag);
  Object.assign(el, props);
  document.body.appendChild(el);
  await (el as unknown as LitLike).updateComplete;
  return el;
}

export function teardown(): void {
  document.body.innerHTML = '';
}
