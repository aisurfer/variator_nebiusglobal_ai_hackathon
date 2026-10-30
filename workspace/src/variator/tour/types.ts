// A guided tour: steps from a config file (config/cases/<case>.tour.json), each highlighting one region of the page
// (a `data-tour` anchor), with a short text, an optional «Try:» line, an optional action the page performs («Show me»)
// and an optional second region to look at («See …»). The tour knows nothing about cases: the host page resolves the
// texts, performs the actions and answers whether an action is done.

/** A region of the page: the element `[data-tour="<anchor>"]`, or the `[data-tour="<focus>"]` inside it (several
 *  focus ids: the first one on the page). */
export interface TourTarget { anchor: string; focus?: string | string[] }

/** An action of the host page, by id, with its arguments (validated against the case config by a unit test). */
export interface TourActionDef {
  id: string;
  args?: Record<string, unknown>;
  /** Label of the «Show me» button (default «Show me»). */
  label?: string;
  /** The region to highlight once the action ran (default: the step's own). */
  then?: TourTarget;
}

export interface TourLinkDef {
  label: string;
  /** A path of the workspace (`/cases/<id>`, `/ask`). */
  to: string;
  /** Opens the tour of that page (`?tour=1`). */
  tour?: boolean;
}

export interface TourStepDef extends TourTarget {
  id: string;
  title: string;
  /** One to three short sentences; `{name}` placeholders are filled from the live page state. */
  body: string;
  try?: string;
  action?: TourActionDef;
  /** A second region of the same step («See the risk calendar»), and back. */
  see?: TourTarget & { label: string };
  /** Keys of the page's help dictionary, offered as «Know more» chips. */
  help?: string[];
  /** A «Know more» chip with a text of this step (a template, filled from the live page like the body). */
  more?: { label: string; text: string };
  links?: TourLinkDef[];
}

export interface TourDef {
  schema: 'variator-tour/1';
  caseId: string;
  /** Minutes of reading, for the first-visit offer («A 3-minute tour of this case»). */
  minutes: number;
  steps: TourStepDef[];
}

/** A step as the tour component shows it: texts filled, actions bound by the host. */
export interface TourStepView extends TourTarget {
  id: string;
  title: string;
  body: string;
  tryText?: string;
  action?: { label: string; then?: TourTarget };
  see?: TourTarget & { label: string };
  /** Host content under the text: «Know more» chips, links to other pages. */
  extra?: React.ReactNode;
}
