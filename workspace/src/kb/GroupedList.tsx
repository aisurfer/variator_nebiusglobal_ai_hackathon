// A compact list of linked records: bullets of one line, grouped by a category, the groups in columns.
// The sources of an answer and the links of a dossier use it.

export interface GroupedItem { id: string; group: string; title: string; mark?: string; note?: string; hint?: string; level?: 1 | 2 | 3 }

const INFLUENCE = { 1: 'Low influence: a loose match, another topic', 2: 'Medium influence: matches the question', 3: 'High influence: on the causal chain' };

export function GroupedList({ items, onOpen }: { items: GroupedItem[]; onOpen: (id: string) => void }) {
  const groups = [...items.reduce((m, x) => m.set(x.group, [...(m.get(x.group) ?? []), x]), new Map<string, GroupedItem[]>())];
  return <div className="kb-source-groups">{groups.map(([group, xs]) => <section key={group}><h4 className="kb-item-kind">{group} <span className="count-pill">{xs.length}</span></h4>
    <ul className="kb-source-list">{xs.map((x, i) => <li key={`${x.id}-${i}`}>{x.level && <span className={`kb-influence kb-influence-${x.level}`} title={INFLUENCE[x.level]} aria-label={INFLUENCE[x.level]}>{'●'.repeat(x.level)}{'○'.repeat(3 - x.level)}</span>}<button onClick={() => onOpen(x.id)} title={x.hint ?? x.title}>{x.mark && <small>{x.mark} </small>}{x.title}{x.note && <small> · {x.note}</small>}</button></li>)}</ul></section>)}</div>;
}
