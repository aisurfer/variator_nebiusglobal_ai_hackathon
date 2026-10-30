// Page-level UI of a case that is not case state (no edit, no recompute): the origin modal. The sections and the
// guided tour reach it through this interface.
export interface CaseUi {
  /** Opens «Where these forks come from». `tour`: opened by the guided tour, without a focus trap (its card stays usable). */
  openOrigin: (opts?: { tour?: boolean }) => void;
  closeOrigin: () => void;
  originOpen: boolean;
}
