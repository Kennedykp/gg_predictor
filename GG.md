\# GG (BTTS) Football Prediction System — NO UI MVP

\#\# 1\. Purpose

This project builds a \*\*GG (Both Teams To Score – YES)\*\* prediction system using:  
\- A Poisson goal model  
\- Free football data  
\- Strict rules  
\- No UI  
\- No agents  
\- No machine learning (initially)

The goal is \*\*process correctness and trust\*\*, not presentation.

\---

\#\# 2\. What This Project Is (and Is Not)

\#\#\# This project IS:  
\- A probability engine  
\- A value-detection tool  
\- A data \+ math system  
\- Terminal / file-output based

\#\#\# This project IS NOT:  
\- A betting platform  
\- A UI product  
\- A “guaranteed wins” system  
\- An AI decision-maker

\---

\#\# 3\. Scope (LOCKED)

\#\#\# Market  
\- GG (BTTS Yes) ONLY

\#\#\# Sport  
\- Football (Soccer) ONLY

\#\#\# Logic  
\- Poisson goal probability ONLY

\#\#\# Data  
\- Free APIs ONLY

\---

\#\# 4\. League Whitelist

\#\#\# Phase 1 — Allowed Leagues  
Use ONLY these leagues initially:

\- English Premier League  
\- Bundesliga  
\- Serie A  
\- La Liga  
\- Ligue 1

Reason:  
\- Stable scoring patterns  
\- Reliable data  
\- Poisson assumptions hold reasonably well

\---

\#\#\# Optional (Phase 2 — After Testing)  
Add only after Phase 1 proves stable:

\- EFL Championship  
\- Bundesliga 2

\---

\#\#\# Explicitly Excluded  
\- International friendlies  
\- Early cup rounds  
\- Youth leagues  
\- Low-tier leagues with missing stats  
\- First-leg knockout matches

\---

\#\# 5\. Data Sources (FREE)

\#\#\# Primary Data Source (Required)  
\- API-Football

Used for:  
\- Fixtures  
\- Team statistics  
\- Home/away goals scored  
\- Home/away goals conceded  
\- League average goals

\---

\#\#\# Odds (Optional but Recommended)  
\- The Odds API

Used ONLY to compute implied probability and value.  
Odds are NOT used for prediction.

\---

\#\# 6\. Required Inputs for GG Calculation

To run the GG Poisson model, you need ONLY:

\- League average goals (per team)  
\- Home team goals scored at home  
\- Home team goals conceded at home  
\- Away team goals scored away  
\- Away team goals conceded away

If any of these are missing → \*\*NO BET\*\*

\---

\#\# 7\. Core GG Formula (DO NOT MODIFY)

\#\#\# Expected Goals

λ\_home \=  
(Home\_GF\_home × Away\_GA\_away) / League\_Avg\_Goals

λ\_away \=  
(Away\_GF\_away × Home\_GA\_home) / League\_Avg\_Goals

\---

\#\#\# GG Probability

P(GG) \=  
(1 − e^(−λ\_home)) × (1 − e^(−λ\_away))

This is the base probability.  
No other model may override this.

\---

\#\# 8\. Value & Decision Rule

\#\#\# Implied Probability from Odds

P\_book \= 1 / Odds

\---

\#\#\# Edge Calculation

Edge \= P(GG) − P\_book

\---

\#\#\# Bet Rule

IF ALL are true:  
\- Edge ≥ 0.05 (5%)  
\- Odds ≥ 1.60  
\- Match passes filters

THEN:  
\- FLAG GG

ELSE:  
\- NO BET

\---

\#\# 9\. Hard Filters (Safety Rules)

GG is NOT allowed if any are true:

\- One team averages \< 1.0 goal  
\- One team keeps \> 40% clean sheets  
\- First-leg knockout match  
\- Heavy favorite vs deep-defending underdog  
\- Missing or unreliable data

Filters are mandatory.  
They protect the bankroll.

\---

\#\# 10\. System Architecture (NO UI)

Python Script / Service  
↓  
Fetch data from API-Football  
↓  
Calculate λ\_home, λ\_away  
↓  
Calculate GG probability  
↓  
Compare with odds (optional)  
↓  
Output result to:  
\- Terminal  
\- CSV  
\- JSON

\---

\#\# 11\. Output Format (Example)

Match: Team A vs Team B

League: Premier League

λ\_home: 1.62

λ\_away: 1.18

GG Probability: 0.56

Odds: 1.80 (Implied: 0.56)

Edge: \+0.00

Decision: NO BET

\#\# 12\. Backend Prompt (NO UI)

\#\#\# COPY & PASTE INTO BUILDER OR AI

Build a backend-only football analytics system.

Requirements:

* No UI  
* No agents  
* No machine learning  
* No betting execution

Responsibilities:

* Fetch match and team statistics from API-Football  
* Calculate GG probability using a Poisson model  
* Optionally fetch odds and calculate value  
* Output results via terminal, CSV, or JSON

Important:

* Do NOT invent betting logic  
* Do NOT modify formulas  
* System is analytics-only

\---

\#\# 13\. Python Logic Prompt (GG Engine)

\#\#\# COPY & PASTE

Write Python code to calculate GG (BTTS Yes) probability using a Poisson goal model.

Inputs:

* league\_avg\_goals  
* home\_goals\_scored\_home  
* home\_goals\_conceded\_home  
* away\_goals\_scored\_away  
* away\_goals\_conceded\_away

Steps:

1. Compute λ\_home and λ\_away  
2. Compute GG probability  
3. Return probability only

Do not add ML or heuristics.

\---

\#\# 14\. Daily Workflow (NO UI)

Correct daily question:  
“Which matches today in approved leagues pass GG rules?”

Steps:  
1\. Fetch today’s fixtures  
2\. Filter by allowed leagues  
3\. Compute GG probability  
4\. Apply filters  
5\. Compare odds  
6\. Log result  
7\. Accept zero-bet days

\---

\#\# 15\. Definition of Success (Early Stage)

Success is:  
\- System runs without errors  
\- Numbers are explainable  
\- Losses make sense  
\- No emotional decisions

Success is NOT:  
\- Daily wins  
\- High accuracy  
\- Many bets

\---

\#\# 16\. When to Add a UI (NOT NOW)

Only add a UI when:  
\- You trust the model  
\- You have logged 100+ predictions  
\- You understand drawdowns  
\- You want convenience, not validation

\---

\#\# 17\. Final Rule

If you cannot trust the output in a CSV,  
you will not trust it in a UI.

Build trust first.

**All betting logic, formulas, and thresholds are fixed and must not be modified unless explicitly instructed.**

END.  
