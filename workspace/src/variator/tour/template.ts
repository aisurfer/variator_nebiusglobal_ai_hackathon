// Tour texts are templates: `{name}` and `{name.part.part}` are filled from the live page state, so no number in a tour
// text is written by hand. An unknown or not-yet-known value reads «…» (the page is still computing).

const PLACEHOLDER = /\{([a-zA-Z][\w.-]*)\}/g;

/** The placeholders of a text, in order («{leader} is best in {pBest}» → ['leader', 'pBest']). */
export function placeholders(text: string): string[] {
  return [...text.matchAll(PLACEHOLDER)].map(m => m[1]);
}

/** Fills the placeholders of a text. `resolve` returns the value of a name, or undefined when it is not known yet. */
export function fillTemplate(text: string, resolve: (name: string) => string | undefined): string {
  return text.replace(PLACEHOLDER, (_, name: string) => resolve(name) ?? '…');
}

/** The text with its placeholders taken out: what a person wrote by hand (the check that it has no digits). */
export function literalText(text: string): string {
  return text.replace(PLACEHOLDER, ' ');
}
