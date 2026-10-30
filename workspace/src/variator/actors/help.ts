// Texts of the «?» popovers of the actors page (secondary texts stay out of the page: the owner's clean-page rule).
export const HELP = {
  page: 'Who the company is and how its actors usually behave, learned from the year of history before the view date, and the register of the risks that already fired. Every number comes from the history; the month model of the cases draws from the same habits.',
  profile: 'Nemotron wrote these sentences from the facts that code prepared and from the actor\'s own records. A checker accepts a sentence only when every number, date and name in it occurs in the facts or records it cites. Each chip after a sentence opens the record of the knowledge base behind it.',
  profileFacts: 'The written profile did not pass the checker, so the card shows the facts themselves.',
  habit: 'A habit is what an actor usually does, how often, and how it reacts: for example, how many days a client takes to pay an invoice. It is learned from the history before the view date; the month model of the cases draws from these numbers.',
  interval: 'The range p1–p99: given the data, the true value lies in this range with 98% certainty. A wide range means few observations.',
  source: 'Where a number comes from, from the most trusted: data (the actor’s own history), group (the cohort of similar actors, when the own history is short), web (market data), llm (general knowledge, until data replaces it), assumption (an expert range).',
  cohort: 'A cohort is a group modelled as one actor: the standard clients, the leads, a future hire of a role. A new member takes the cohort habits until it has its own history. Future hires are always cohorts, never named people.',
  changes: 'The decisions, the hypotheses about the world and the shocks of cases A and B that act on this actor.',
  people: 'A person card shows work habits only: no salary and no personal chance of leaving. The staff rates are on the future-hire cohorts.',
  risks: 'Events of the history that hurt or helped the company, counted by kind. Per month: the base rate with its p1–p99 range. Shock in the cases: the case items that model the risk, so its chance can be changed in a case.',
} as const;
