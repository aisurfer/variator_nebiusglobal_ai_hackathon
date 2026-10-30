// «Open this future in the knowledge base»: a day example is a KB branch (its own tenant, WP19). The workspace opens it
// once the knowledge-base screens list the branch (WP15: `branches` of the company in /api/kb/organizations) and take
// `?branch=<tenant>` on the brief, Ask, search and the dossiers. Pure: tests/case-actors.test.ts.

export interface KbBranch { tenant: string; label?: string; fork?: string; kind?: string }

/** The branches a company lists (none while the KB screens cannot open one). */
export const branchesOf = (org: unknown): KbBranch[] => {
  const b = (org as { branches?: unknown } | null)?.branches;
  return Array.isArray(b) ? b.filter((x): x is KbBranch => !!x && typeof (x as KbBranch).tenant === 'string') : [];
};

/** Whether the example's branch can open, the URL parameters that open it, and the reason when it cannot. */
export function branchLink(tenant: string | undefined, org: unknown): { open: boolean; params: Record<string, string | null> | null; why: string } {
  if (!tenant) return { open: false, params: null, why: 'This example is not a knowledge-base branch yet: the day files of the forward run make one per example.' };
  const listed = branchesOf(org).some(b => b.tenant === tenant);
  if (!listed) return { open: false, params: null, why: `This future is the knowledge-base branch «${tenant}». The knowledge-base screens open it once they list it.` };
  return { open: true, params: { branch: tenant, entity: null, metric: null }, why: '' };
}
