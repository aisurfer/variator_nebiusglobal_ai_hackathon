# variator.research

Web facts with checked citations (Lightning plans and extracts, Tavily searches).

| Need | Use |
|---|---|
| Business profile | `research_business` MCP tool -> a `research*` handle |
| Facts for a question | `gather_external_facts` -> `ResearchBundle` |
| Tavily client and cache | `TavilyClient`, `TavilyCache` (env `TAVILY_API_KEY`, `VARIATOR_TAVILY_OFFLINE`) |
| Offline tests | `research.testing.FakeTavily` |

Citation rules, offline demo, credits: `docs/knowledge/research.md`.

`research_business` starts with a reproducible lens set covering the business model,
customers, competitors, regulation, operations, labour, risks, benchmarks, market,
technology and costs. `max_queries` defaults to 24 and is capped at 1000. The large
budget is explicit because every uncached Tavily search spends credits. The resulting
checked facts can be passed to `case_from_question(research=...)`.

For actor and risk cards, save the same bundle from the CLI and pass it to the cards
build:

```bash
python -m variator.research.cli \
  --business "IT services company" --geography "United Kingdom" \
  --out /tmp/business-research.json
python -m variator.actors.cards.cli all --work /tmp/actor-cards \
  --research /tmp/business-research.json
```
