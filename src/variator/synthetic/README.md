# variator.synthetic

Demo data of one fictional company (cases `warehouse`, `price_increase`, `new_market`).

| Need | Use |
|---|---|
| Generate or load a case | `generate_demo_case`, `load_demo_case`, `write_demo_case` |
| Regenerate `data/demo` | `python -m variator.synthetic --out data/demo` |
| Text columns (Data Designer + Lightning) | `build_commentary_config`, `run_designer`, `add_text_tables` |

Facts and provider policy: `docs/knowledge/synthetic.md`. IT outsourcing data: `variator_it_services.jira_synth`.
