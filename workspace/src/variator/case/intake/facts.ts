// The company facts of the intake chat: config/company.facts.json. scripts/mvp/make_company_facts.py writes it from the
// company history spec (src/variator/orgsim/company/spec.py), and tests/mvp/test_company_facts.py checks that the
// two agree, so the agent cannot say old facts. Do not edit the JSON file by hand.
import facts from '../../../../config/company.facts.json';
import type { IntakeFactsFile } from './types';

export const COMPANY_FACTS: IntakeFactsFile = facts as IntakeFactsFile;
