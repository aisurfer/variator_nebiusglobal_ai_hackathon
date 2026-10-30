# variator.actors.cards

Actor cards (plan C2-C3, PRD S6): a cited profile over the WP10 habits, and the register of the risks that already fired. Code makes every number; Nemotron Ultra extracts the card from the facts and the actor's own records before T (a profile and behaviour traits, each citing what it says); a checker rejects any unsupported fact.

| Need | Use |
|---|---|
| Run (resumable) | `python -m variator.actors.cards.cli {build,ids,write,artifacts,export,append,all} --work DIR` |
| Card plan | `build.build_plan(actors, history, cases)` |
| Case items per actor | `changes.TOUCHES`, `changes.RISK_SHOCKS` |
| Extractor, checker | `writer.CardWriters.run` (model `ROLE_MODEL["actor_cards"]`), `check.check_card` |
| Records of a card | `records.records_for(history, cards, known)`; the CLI keeps them in `<work>/writer/records.json` |
| Artifact set | `artifacts.write_set` (manifest `kind: actor-cards`) |
| External context | `--research research.json` adds checked Tavily facts to actor cards and risks |
| KB delivery | `export.export_delivery`, `export.intake_validate` |
| Tests | `tests/actors/test_cards.py`, `tests/actors/test_card_extractor.py` |

Details (rules, KB pages, append): `docs/knowledge/company-learning.md`. Page: `workspace/src/variator/actors/`.
