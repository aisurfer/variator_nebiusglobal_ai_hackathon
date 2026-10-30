"""Prompts for ingestion calls.

Each prompt is a static instruction block followed by a template for the
dynamic part. Callers send ``INSTRUCTIONS + "\\n\\n" + TEMPLATE.format(...)`` as
one user message: Token Factory caches repeated prefixes, so the static text
must come first. Instructions go in the user turn rather than a second system
message because ``TokenFactoryClient.structured`` already sends a system message
with the JSON schema, and some chat templates reject two system messages.
"""

PROFILE_FACTS_INSTRUCTIONS = """\
You turn a statistical profile of a company data table into facts for a business \
decision model.

Rules:
- Each fact is one self-contained sentence a reader understands without the table: name \
the metric, the table's subject, the period and the unit.
- Put the single key number of the fact in "value", copied exactly as it appears in the \
profile (same digits; percentages as shown, e.g. 12.3 with unit "%"). Never compute new \
numbers (no averages, differences or ratios that are not in the profile).
- Prefer facts that matter for decisions: totals and levels, growth (YoY, CAGR), \
volatility (coefficient of variation), seasonality peaks and troughs, mix of categories, \
and data-quality problems (missing data, partial periods, unparseable cells).
- Set "column" to the profile column the fact is about, or null for table-wide facts.
- Set "period" like "2025-01..2025-12", "last 12 months to 2026-08" or "FY2025" when known.
- "confidence" is 0..1: lower it for facts based on few rows or flagged data problems.
- Do not invent facts the profile does not support."""

PROFILE_FACTS_TEMPLATE = """\
Return at most {max_facts} facts.

{profile}"""

TEXT_FACTS_INSTRUCTIONS = """\
You extract facts from an excerpt of a company document (annual report, board memo, \
market study, email) for a business decision model.

Rules:
- Each fact is one self-contained sentence a reader understands without the document: \
name the company or market, the metric, the period and the unit. Resolve pronouns.
- Put the single key number of the fact in "value", copied exactly as written in the text, \
with its unit in "unit" (e.g. value 4.2, unit "million EUR"; value 12.5, unit "%"). Use \
null when the fact has no number. Never compute new numbers.
- Extract only what the text states. Mark forecasts, targets and plans as such in the \
statement and give them lower confidence than reported actuals.
- Prefer facts that matter for decisions: revenue, costs, margins, volumes, prices, growth, \
capacity, headcount, market size, risks, constraints, deadlines and stated plans.
- "confidence" is 0..1: 0.8-0.9 for reported figures, 0.5-0.7 for estimates, 0.3-0.5 for \
targets or vague claims.
- If the excerpt contains no relevant facts, return an empty list."""

TEXT_FACTS_TEMPLATE = """\
Source: {source} (part {part} of {parts})
Return at most {max_facts} facts.

<excerpt>
{text}
</excerpt>"""

CHART_READING_INSTRUCTIONS = """\
You read business charts, dashboards, tables and scanned report pages.

Describe the image as structured data:
- is_chart: false if the image holds no chart, table or figures (then leave the rest empty).
- chart_type: line, bar, stacked_bar, area, pie, scatter, table, kpi_tiles or other.
- title, x_axis and y_axis (label and unit) exactly as printed; null if absent.
- series: one entry per line, bar group or table row/column with its points (x label as \
printed, y as a number). Read printed data labels exactly; when you must estimate a value \
from gridlines, say so in caveats.
- annotations: every number or short label printed on the image (data labels, totals, KPI \
tiles, footnotes), verbatim.
- takeaways: 2-6 self-contained statements a business reader should notice (trend, peak, \
drop, share, gap between series). Put the key number in "value" exactly as read from the \
image, with its unit and period. Do not compute numbers that are not on the image.
- caveats: legibility problems, truncated axes, estimated values."""

CHART_READING_TEMPLATE = "Source: {source}"

QUESTION_INSTRUCTIONS = """\
You help a business leader frame a decision before it is simulated with Monte Carlo.

From the leader's question (and known facts, if any) produce:
- restated_question: the decision in one neutral sentence.
- options: 2-5 mutually comparable alternatives. Always include the status quo or \
"do nothing" baseline when it is a realistic choice. name is a snake_case identifier \
(e.g. open_second_warehouse); description says what the option means in practice.
- metric: the outcome to compare options on (e.g. "3-year profit contribution", "NPV", \
"EBITDA 2027"), with metric_unit (currency code or unit) and higher_is_better.
- horizon_months: the evaluation horizon in months (use the question's horizon; otherwise \
a sensible default for this kind of decision, usually 12-60).
- constraints: hard limits stated or clearly implied (kind: budget, deadline, capacity, \
risk, policy or other), with a number and unit when given.
- key_uncertainties: the uncertain drivers that will decide the outcome (demand growth, \
price, costs, timing, competitor reaction...).
- missing_information: short, specific questions to ask the leader for numbers or \
choices that are needed but unknown. Do not ask about anything the facts already answer.
Do not invent numbers that neither the question nor the facts contain."""

QUESTION_FACTS_TEMPLATE = """\
Known facts:
{facts}"""

QUESTION_TEMPLATE = """\
Leader's question:
{question}"""
