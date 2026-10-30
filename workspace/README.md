# VARIATOR workspace

The web workspace of the VARIATOR MVP: an Express server and a React UI. It shows one company to its CEO: the morning
brief with every metric, metric sheets, entity dossiers, questions to the knowledge base and the state of the data
sources, all read live from the knowledge base (`kb/`). New VARIATOR screens (cases, forks, month, week, day) are added
here.

## Provenance

Copied from the Plancy dashboard (`magic_button/plancy_dashboard`, branch `kb-backend`,
commit `5e32db0d96e1e16eee3f3c1a0399b405bb868393`) on 2026-10-27. The interface and the design are Plancy's: same
fonts (DM Sans), colours, layout and components. Changes: the VARIATOR wordmark and titles, one company instead of an
organisation switcher, one user role (the CEO) instead of a role switch, English only, a proxy to the VARIATOR API, and
everything the MVP does not use removed.

## What is here

| Path | What |
|---|---|
| `src/App.tsx` | The shell: sidebar (brand, company, navigation, data sources, profile of the CEO), top bar (view date, knowledge-base state), metric and dossier sheets |
| `src/pages.tsx` | Page registry. A new screen is one entry: id (= first path segment), label, icon, sidebar group, `render(context)` |
| `src/routes.ts`, `src/early.ts`, `src/firstView.ts` | The first view (WP29): the code of each page as a chunk of its own, the first view's requests started before that code arrives, and the early answers the pages take (see «The first view») |
| `src/kb/` | Knowledge-base pages: `KbBrief` (the CEO brief: top row and areas), `catalogue.ts` (the brief and drill-down from the VARIATOR metric catalogue), `KbDossier` (metric sheet, entity dossier), `KbAsk`, `KbSources`; API client and shared types |
| `src/genui/primitives/` | GenUI primitives: atoms, molecules, the 11 organisms (KPI tile, trend, breakdown, index, exception list, heatmap…), formatting and brief rules |
| `src/genui/detail/` | Metric drill-down (`MetricDrilldown`): metric → step → aggregate → records → source, over a `DrilldownSource` the host provides |
| `src/genui/contracts.ts` | The result contract the primitives and the drill-down render: `MetricSpec`, `MetricResult`, `LineageNode`, `RecordRef`, `WidgetSpec` |
| `server/app.ts`, `server/index.ts` | Express app and bootstrap: `/api/health`, `/api/kb/*`, `/api/variator/*`, the built UI with a single-page fallback |
| `server/preload.ts` | The first view's API requests of an address, named in its `index.html` (`<link rel="preload" as="fetch">`) |
| `server/kb/` | Knowledge-base client, read model (snapshot), the brief metrics and their layout (`metrics.BRIEF`; formulas, never a model), router |
| `server/variatorProxy.ts` | Pass-through proxy to the VARIATOR API |
| `config/company.json` | The company: Larchwood Systems, tenant `company`, one user role (the CEO), North Star |
| `tests/` | Unit and API tests (`node:test` via `tsx`); `tests/e2e/` Playwright checks against a live knowledge base |

Kept from Plancy: the shell, the knowledge-base screens and server modules, the GenUI primitives, the metric
drill-down, the result contract, the stylesheets of these (rules of removed components pruned).

Removed: the demo workspace (overview, domain pages, executive brief, decision drafts, global search, help dialog,
demo organisations and their SQLite database), the KB metric studio and the GenUI metric studio (chat, canvas, spec
window), the generative morning dashboard and its sample-data generator, the primitive gallery, the GenUI metric engine
over demo data, the LLM agents (Metric Analyst, Dashboard Composer, KB analyst), their prompts, LLM key loading and
recorded fixtures, the seed/import/record/demo scripts, `data/markdown`, `data/organizations`, `data/history`,
`data/genui`, and their tests. Nothing is stored locally any more: the server has no database.

## The company and the user

`config/company.json` (or the file named by `COMPANY_FILE`) holds the one company: `id` and `tenant` `company`, name
Larchwood Systems, currency USD, time zone, engineering department, the North Star read from weekly report pages, and
the example questions of the «Ask» page. The MVP has one user role: `ceo`, Avery Collins, Chief Executive Officer,
principal `user:admin` (full access). The file keeps `roles` as a list, so a later release can add roles. The head of
finance and the head of delivery stay people of the company (knowledge-base persons, owners of levers), not users.

The shell has no role switch and no `role` URL parameter; an old link with one still opens the CEO brief. The API takes
`role` as optional: it defaults to the only role, `role=ceo` works, any other value gives 400.

The CEO brief (`server/kb/metrics.ts`, `BRIEF`) shows every metric once:

| Place | Metrics |
|---|---|
| Top row | North Star, invoiced to clients, client letters awaiting a reply, team activity index, open high-priority bugs |
| Money | Supplier bills, contracts and amendments |
| Clients and growth | First reply to clients, open proposals, client escalations, prospects without contact |
| Delivery and service | Tickets done, cycle time, work in progress, merge requests waiting, reopened, work on changed grounds |

The headline is one CEO text: the North Star, then one sentence per area. Open proposals read the sales deals of a
simulated history (stage proposal or negotiation on the date), else Plancy proposal documents. `metrics.OFF_BRIEF`
keeps a metric off the brief when this history cannot feed it (discounts and term changes: always 0 here); it still
opens by id.

Page texts follow one rule: titles, numbers, controls, the headline and warnings stay visible; intros, how a number is
computed, notes and long captions sit behind a «Know more» button (`genui/detail/shared.KnowMore`, lucide `Info`) or in
a disclosure closed by default (`Block closed`).

The brief has two sources (`src/kb/KbBrief.tsx`, `src/kb/catalogue.ts`):

1. The VARIATOR metric catalogue (`src/variator/metrics/catalog`, 23 metrics). The page lists the artifact sets
   (`GET /api/variator/artifacts`) and reads `briefs/ceo.json` of a set with manifest `schema` 2 whose `asOf` is the
   view date. A tile opens `MetricDrilldown` on `metrics/<id>.json`: value, weekly series, formula, steps,
   parameters, and the records behind the number; a record opens its knowledge-base dossier.
2. The brief computed here from the knowledge base (`/api/kb/<org>/brief`), when the VARIATOR API is down, has no set
   with a CEO brief, or no set for the view date. A quiet note under the headline says why.

The North Star reads the latest page labelled `report` and its property `ARR, $k` (secondary: `Active clients`,
`New ARR, $k`, `Churned ARR, $k`, `Cash, $k`; period: `Period`).

## Simulated futures (branches)

Each example future of a case fork is its own knowledge-base tenant (WP19: `company-b-<fork>-p10|p50|p90`).
`GET /api/kb/organizations` lists the loaded ones as `branches: [{tenant, caseId, fork, kind, percentile, label, from}]`
(`server/kb/branches.ts`: candidates from `config/cases`, kept when the base has the tenant and its projections have
caught up). Every `/api/kb/:org/*` route takes `?branch=<tenant>`; an unlisted branch gives 400. The read model is
cached per tenant. A branch's view dates run from its fork date to its last simulated day.

URL parameter `branch` (for example `/brief?branch=company-b-pay_tech_debt-p50`): the brief, «Ask», the data sources
and the dossiers read the branch (`kbApi` adds the parameter to every call). A Simulation banner names the branch and
says it is not real history; «Back to the company» leaves it; the case pages drop it. The brief of a branch starts
with «Simulated future: …», never reads the history catalogue, and shows a number as unknown (not zero) when the
branch has no records of its kind (a simulation has no mail, Jira or GitLab).

## The as-of rule

The workspace has one view date (URL parameter `asOf`, default the last day of data). Everything on a date is computed
from what the knowledge base knew at the end of that day: versions recorded by then, causal candidates known by then,
links stale then. A sheet of a date equals the sheet of a knowledge base loaded only up to that date
(`tests/kb-asof.test.ts`). Questions are asked with `now` = the end of the day and `known_at` = the same cut.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `HOST` | `0.0.0.0` | Address of the workspace server: `0.0.0.0` for the container; `HOST=127.0.0.1` keeps a dev server on this machine only |
| `PORT` | `3002` | Port of the workspace server (UI and `/api`) |
| `KB_URL` | `http://127.0.0.1:8720` | Knowledge-base API (kb-api) |
| `KB_TIMEOUT_MS` | `60000` | Timeout of a knowledge-base call (questions: 180 s) |
| `KB_SERVICE_PRINCIPAL` | `user:admin` | Principal of the organisation-wide read model and the source status |
| `COMPANY_FILE` | `config/company.json` | The company configuration |
| `KB_PREWARM` | on | After the start, read the branch list, the dates, the brief and the status of the company in the background, so the first visitor gets them from the cache; `0` turns it off |
| `VARIATOR_API_URL` | `http://127.0.0.1:8730` | VARIATOR API (`src/variator/web`); `/api/variator/<path>` goes to `${VARIATOR_API_URL}/api/variator/<path>` |
| `VARIATOR_TIMEOUT_MS` | `30000` | Time to wait for the VARIATOR API's response headers; a streamed body is not cut |
| `VARIATOR_BODY_LIMIT` | `10mb` | Largest request body passed to the VARIATOR API |
| `API_PORT`, `VITE_PORT` | `3002`, `5173` | Development only: `npm run dev` proxies `/workspace/api` of the Vite server to `API_PORT` |

The proxy keeps the method, the full path and query, the headers (without hop-by-hop ones) and the body; the answer
(status, headers, body) is streamed back. A VARIATOR API that is down gives `502`, one that does not answer in time
`504`, both as JSON `{ "error", "service": "variator-api", "url" }`. `/api/health` answers without either service.

## The base path /workspace/

The whole app lives under `/workspace/` on its site (WP27); the root of the domain holds the landing page.

- Vite `base: '/workspace/'` (`vite.config.ts`): the built pages, assets and the worker load from `/workspace/...`.
- The router takes the base off (`basename` in `src/main.tsx`). In-app paths in code, `config/` (tours, cases) and
  artifact sets stay `/brief`, `/cases/<id>`; `navigate()` and `ctx.go` take them as they are.
- `src/appUrl.ts`: `apiUrl('/api/...')` for every fetch, `appHref('/brief?...')` for every `<a href>`
  (`ctx.link` returns an href with the base), `appPath(window.location.pathname)` back to the in-app path.
- The server (`server/basePath.ts`) answers the same routes with and without `/workspace`: caddy takes the base off
  (`handle_path`), a direct visit keeps it. An address outside the base opens the same page under it (`main.tsx`).
- Dev: http://127.0.0.1:5173/workspace/; the stack: http://127.0.0.1:8088/workspace/.
- Playwright: `PLAYWRIGHT_BASE_URL` is the origin; the config appends `/workspace/`. Specs use relative paths
  (`page.goto('brief')`, `request.get('api/kb/...')`): a path with a leading `/` would drop the base.

## The first view

NFR7: the first view in under 3 s over the internet (WP29). The entry chunk holds the shell and the brief (the first
page of most visits) and loads `react` (React, the router) as a chunk of its own; every other page loads when it opens
(`React.lazy` in `src/pages.tsx`, its own loading state meanwhile). The libraries that only some pages use are chunks of
their own (`vite.config.ts`): `charts` (recharts: the case page and the metric sheet), `markdown` (Ask and the
dossier), `dialog` (the sheets). The metric and dossier sheets load with the first sheet asked for, or once the brief
is on screen. The stylesheet stays one file (`cssCodeSplit: false`): the cascade is the same on every page.

The first view does not wait for its code to ask for its data:

- `server/preload.ts`: the server puts `<link rel="preload" as="fetch" crossorigin>` for the requests it can name into
  `index.html` (the company, its dates, the state of the sources; the list of artifact sets on the brief, the actors
  and the case pages): the browser sends them with the page.
- `src/firstView.ts` (`main.tsx`, before the first render): asks for them again (the browser hands over the preloaded
  answers) and for the ones that need their answers: the brief file of the catalogue set of the view date; on a case
  page the code of the page, the case's files, the month file and the day file; on the actors the card files.
- `src/early.ts`: the pages read through `earlyFetch`, which takes an early answer once (the list of artifact sets:
  every reader in the first seconds) and otherwise goes to the network as before.

`index.html` is sent with `Cache-Control: no-cache` (a new release shows at once); the files under `assets/` have
content hashes and are kept for a year (`immutable`). After a release, a page that asks for a file of the old one
reloads once (`vite:preloadError` in `main.tsx`). The fonts (DM Sans, `@fontsource`) are served from the site with
`font-display: swap`: they never block the text.

## Run and check

```bash
npm ci                      # once
npm run dev                 # server on 3002 (tsx watch) and Vite on 5173, both on all addresses
npm run typecheck           # tsc --noEmit
npm test                    # unit and API tests (node:test via tsx), no services needed
npm run build               # typecheck and vite build into dist/
npm start                   # serve dist/ and /api on PORT
KB_E2E=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:3002 npm run test:e2e   # needs Playwright browsers and a loaded kb
```

UI only, against a deployed site (no local stack, hot reload; UX work):

```bash
API_TARGET=https://variator.trijet.tech npx vite --host 127.0.0.1   # http://127.0.0.1:5173/workspace/
```

Docker: `docker build -t variator-workspace workspace/` (build context `workspace/`); the image serves on 3002 with a
health check on `/api/health`. Set `KB_URL` and `VARIATOR_API_URL` to the service addresses of the compose network.
The image does not set `HOST`: the default `0.0.0.0` lets the port mapping reach the server.

Case pages (`/cases/<case>`, data from `config/cases/`) in development, on this machine only. The ports 3003 and 5174
keep the dev servers apart from the workspace container of the MVP stack (3002) and from `npm run dev`:

```bash
HOST=127.0.0.1 PORT=3003 npm run dev:server                  # server on 127.0.0.1:3003 (tsx watch)
API_PORT=3003 VITE_PORT=5174 npx vite --host 127.0.0.1       # UI on http://127.0.0.1:5174/workspace/, /api to 3003
PLAYWRIGHT_BASE_URL=http://127.0.0.1:5174 npx playwright test case.spec.ts   # case page checks
```

The CEO brief from the metric catalogue in development, on this machine only (no Docker; port 8730 is the stack's):

```bash
uv run python -m variator.metrics.catalog.cli compute --history <history dir> --out <artifacts>/<set> --as-of <last day of data>
VARIATOR_ARTIFACTS_DIR=<artifacts> uv run uvicorn variator.web.app:app --host 127.0.0.1 --port 8731   # from the repo root
HOST=127.0.0.1 PORT=3022 VARIATOR_API_URL=http://127.0.0.1:8731 npm run dev:server
API_PORT=3022 VITE_PORT=5183 npx vite --host 127.0.0.1                         # UI on http://127.0.0.1:5183/workspace/
KB_E2E=1 PLAYWRIGHT_BASE_URL=http://127.0.0.1:5183 npx playwright test kb.spec.ts          # shell and brief checks
```

## Adding a screen

Add a `PageDef` to `src/pages.tsx` (a new sidebar group for VARIATOR screens; its code in `PAGE_CODE` of
`src/routes.ts`, rendered through `lazy`, with a `fallback` like its own loading state) and render it from the shell
context:
`org`, `role` (the user: the CEO), `asOf`, `dates`, `go`/`link` (keep `asOf` in the URL), `openEntity` (dossier sheet),
`openMetric` (metric sheet). Build it from the shell classes (`page-intro`, `card`, `section-heading`, `badge`,
`metric-card`…) and the GenUI primitives inside a `.g-scope` element; drill into a metric result with
`MetricDrilldown`. Call the VARIATOR API through `apiUrl('/api/variator/...')`; give links `appHref` or `ctx.link`.
