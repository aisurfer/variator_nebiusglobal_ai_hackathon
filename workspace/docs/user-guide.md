# 🧭 VARIATOR: user guide

VARIATOR lets a manager test a decision **on a model of their own company** before they make it. You see **which option wins**, **how often** it wins, **what can go wrong** and **what to do then**. You leave with **a decision, the signs to watch and a backup plan**, not only charts.

## 🌅 Brief: know where the company stands this morning
- The **key numbers** of money, clients and delivery, and **what changed** since last week.
- Each number explains itself: the question it answers, how it is counted, its target, and the **public definition** it follows.
- The **warning signs** of your decisions: the Brief tells you when the company starts to move along the path you feared, **in time to start the backup plan**.

## 💬 Ask: get the reasons, not only the numbers
- Ask **why** something happened. The answer **cites** the letters, tickets, meeting notes and documents it rests on.
- Each source shows **how strongly it supports** the answer, so you know what to read first.
- The chain to the root cause separates the **real cause** from the place where a decision was only discussed.

## 🔀 Cases: test a decision before you make it
1. **Forks: your options.** Compare the options side by side. Add what may happen around you (for example, a supply shortage or the loss of a large client) and see **which option still holds**. Move decisions in time and see **the effect at once**.
2. **Month: the answer.** The **best option**, **how often it wins**, and **the month each risk can fire**, with the choice that reduces it.
3. **Week: how the year can unfold.** The **common paths** and the **bad branches** of the options you shortlist. On a bad branch, set an **early warning sign** and prepare a **backup plan**; the case shows at once **what the backup plan saves**.
4. **Day: what it feels like.** Example days of a **bad, a typical and a good** future. On a bad day, **assign a fix** to an owner, or record that **too much depends on a single person**.

## ✅ Your decision: from analysis to action
- **Commit** the option you choose, with its warning signs, backup plans, fixes and key-person risks.
- After the commit, the Brief **checks the warning signs against the real numbers** every morning.

## 🔎 Why the results are trustworthy
- **Monte Carlo simulation.** Each option runs through 1,000+ possible futures. All options meet **the same futures**, so the comparison is **fair**.
- **Uncertainty is shown, not hidden.** Results are **ranges** (8 in 10 futures fall inside) and **shares** ("best in 7 of 10 futures"), not single guesses. When 2 options are **too close to call**, the page says so.
- **Learned from your history.** The model of the company comes from **its own records**: the people, the clients and the past events.
- **Every number has a source.** A public definition, the company's records, or the project configuration.
- **Simulations stay apart.** A simulated future is **always labelled** and **never mixes** with the real history.

### 🧪 How well VARIATOR is tested
The tests use test companies with a known history, so every answer is **checked against the truth**. **Real companies will differ.**

**How we check it**
- **Same answer.** **500 random seeds** per case; **the same option** must win.
- **Forecasts against the past.** Hide the last months, forecast them, **count the hits inside the range**.
- **Learning against the truth.** VARIATOR must find the **hidden habits** of a test company.
- **Sound choices.** Removing a loser, weighting a goal or nudging inputs must **not flip the winner**.
- **Answers against facts.** Numbers must stand **in the cited sources**; reason chains must **match the known causes**.

**What the tests show**
- The same option wins in **99.6%** of 500 runs (a clear case) and in **88%** (a close call; **98%** with 1,000 futures per run).
- **Good:** a range promises to catch 8 of 10 real outcomes, and it **catches 8 of 10** (80%).
- Range width: revenue **3%** of the value, cash **19%**, churn **still very wide**.
- **96%** of the hidden habits are found.
- The best option **stays the best** when you drop a weaker option or change your inputs by up to 5%. When you give a goal more weight, the option that is best for that goal **never falls in rank**.

**How reliable the forecasts are**
- The **best option** and the **revenue and cash ranges** are reliable for a decision. A close call means the 2 options are **about equal**: other reasons decide.
- Read churn as **a direction, not a number**.
- **Check a reason chain** against its sources before you act.

**What we test next**
- **1,000+ case variants**, on every new version.
- **Real company** histories.
- Answers written by the **language model**.
