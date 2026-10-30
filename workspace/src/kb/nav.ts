// The links of the workspace navigation (sidebar, brand, data sources). Pure, unit-tested in tests/kb-nav.test.ts.

/** The link to a page from the navigation: only the view date (`asOf`) and the simulated future (`branch`) go
 * with it, never the parameters of the page it leaves (`metric`, `entity`, `actor`, `tab`, `q`…). A page outside
 * the simulated future (`keepBranch` false) leaves the branch and its dates. */
export function navHref(id: string, search: string, keepBranch: boolean): string {
  const cur = new URLSearchParams(search), next = new URLSearchParams();
  const branch = cur.get('branch'), asOf = cur.get('asOf');
  if (branch && keepBranch) next.set('branch', branch);
  if (asOf && (keepBranch || !branch)) next.set('asOf', asOf);
  const q = next.toString();
  return `/${id}${q ? `?${q}` : ''}`;
}
