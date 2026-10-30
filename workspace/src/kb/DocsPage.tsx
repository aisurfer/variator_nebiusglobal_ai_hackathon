// The Documentation page: the end-user guide (workspace/docs/user-guide.md), bundled with the app. It has the frame of
// the other pages: the page intro (the guide's title and first paragraph), then one card per «## » section.
import ReactMarkdown from 'react-markdown';
import guide from '../../docs/user-guide.md?raw';

const [head, ...parts] = guide.split(/^## /m);
const title = (head.match(/^# (.*)$/m)?.[1] ?? 'Documentation').replace(/^\W+\s*/u, '');
const lead = head.replace(/^# .*$/m, '').trim();
const sections = parts.map(p => {
  const [first, ...rest] = p.split('\n');
  return { heading: first.trim(), body: rest.join('\n').trim() };
});

export function DocsPage() {
  return <div className="kb-docs">
    <section className="page-intro"><div><div className="eyebrow intro-date">DOCUMENTATION<span>·</span>USER GUIDE</div>
      <h1>{title}<span className="lime-period">.</span></h1>
      <p className="muted">{lead}</p></div></section>
    {sections.map(s => <section key={s.heading} className="card kb-panel">
      <div className="section-heading"><div><h2>{s.heading}</h2></div></div>
      <div className="kb-pad kb-docs-body"><ReactMarkdown>{s.body}</ReactMarkdown></div>
    </section>)}
  </div>;
}
