// Key phrases of the last answer of «Ask the knowledge base», set in bold in the source text of a dossier.
// No LLM: the terms are the content words, names and numbers of the question and the answer.

const STOP = new Set('the a an and or of to in on at for by with from as is are was were be been it its this that these those what which who whom how why when where do does did not no yes we our you your they their he she his her them there here than then so if but about into over under after before up down out per via all any each more most less some such can could will would should may might must has have had also only just very week weeks month months day days year years'.split(' '));

let terms: string[] = [];

/** Keep the key terms of a question and its answer for the dossiers opened from the answer. */
export function setAskTerms(question: string, answer: string) {
  const words = `${question} ${answer.replace(/\[\d+\]/g, ' ')}`.match(/\$?\d[\d.,]*%?[kKmM]?|[\p{L}][\p{L}\p{N}'-]{2,}/gu) ?? [];
  terms = [...new Set(words.map(w => w.replace(/[.,]$/, '')).filter(w => !STOP.has(w.toLowerCase()) && (w.length > 3 || /\d/.test(w))))]
    .sort((a, b) => b.length - a.length).slice(0, 40);
}

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/** Markdown with each key term in bold; tables, links and code keep their syntax. */
export function highlightTerms(markdown: string): string {
  if (!terms.length || !markdown) return markdown;
  const re = new RegExp(`(?<![\\p{L}\\p{N}*])(${terms.map(escape).join('|')})(?![\\p{L}\\p{N}*])`, 'giu');
  // only the prose: not headings, not the targets of links, not code
  return markdown.split('\n').map(line => /^\s*(#|```|\|\s*-)/.test(line) ? line
    : line.split(/(\]\([^)]*\)|`[^`]*`)/).map((part, i) => i % 2 ? part : part.replace(re, '**$1**')).join('')).join('\n');
}
