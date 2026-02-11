## **Project: Run-3 (Unanswered Goals) Market — R3-NO Focus**

### **Context (READ CAREFULLY)**

You are extending an existing football analytics project called **gg\_predictor**.

The GG (BTTS) market already exists and **MUST NOT be modified**.

You are to **ADD a NEW, SEPARATE MARKET MODULE** inside the same repository.

This new market is:

**“Any team to score 3 goals consecutively (unanswered)”**  
Primary focus: **NO (does NOT happen)**  
Secondary focus: **YES (rare)**  
---

## **🚫 ABSOLUTE RULES (NON-NEGOTIABLE)**

1. **DO NOT modify ANY existing GG files**  
2. **DO NOT reuse GG logic**  
3. **DO NOT merge GG and Run-3 logic**  
4. **DO NOT change data sources**  
5. **DO NOT optimize or simplify the math**  
6. **DO NOT restrict leagues**  
   → Use **ALL football fixtures worldwide** available for the day  
7. **Singles only logic** (no parlays, no rollovers)  
8. **No UI** — backend / CLI / JSON output only

Violation of any of the above \= incorrect implementation.

---

## **🧱 REQUIRED PROJECT STRUCTURE**

Create a **new folder** inside gg\_predictor:

gg\_predictor/

├── gg/                    \# EXISTING — DO NOT TOUCH

│

├── run3/                  \# NEW MODULE

│   ├── run3\_probability.py

│   ├── run3\_filters.py

│   ├── run3\_decision.py

│   ├── main\_run3.py

│

├── shared/                \# EXISTING

│   ├── api.py             \# reuse data access

│   ├── config.py

│   └── [utils.py](http://utils.py)

The run3/ module must be **fully isolated** from GG logic.

---

## **📊 DATA REQUIREMENTS**

Use the **same data source and pipeline** already used by GG:

Required inputs per match:

* lambda\_home  
* lambda\_away  
* Fixture metadata (teams, league, datetime)

No additional APIs.

No new scraping.

---

## **🎯 MARKET DEFINITION (VERY IMPORTANT)**

### **Event:** 

### **R3 (Run-3)**

R3 \= TRUE if **either team scores 3 goals consecutively without reply** at any point in the match.

Examples:

* 3–0 → YES  
* 0–3 → YES  
* 2–1 → NO (sequence broken)  
* 1–1–1 → NO  
* 0–1–2–3 → YES

Primary interest \= **R3 \= NO**

---

## **🧮 CORE MATHEMATICAL LOGIC (DO NOT ALTER)**

### **Step 1: Goal-share probabilities**

p\_home \= lambda\_home / (lambda\_home \+ lambda\_away)

p\_away \= lambda\_away / (lambda\_home \+ lambda\_away)

### **Step 2: Probability a team scores 3 in a row**

P\_home\_run3 ≈ p\_home³

P\_away\_run3 ≈ p\_away³

### **Step 3: Probability ANY team scores 3 in a row**

P\_R3\_YES \= 1 \- (1 \- P\_home\_run3) \* (1 \- P\_away\_run3)

### **Step 4: Probability of interest (NO)**

P\_R3\_NO \= 1 \- P\_R3\_YES

## 

## **🧱 HARD FILTERS (MANDATORY)**

### **❌ DISALLOW ALL BETS if ANY are true:**

* lambda\_home \+ lambda\_away ≥ 3.5  
* p\_home ≥ 0.65 OR p\_away ≥ 0.65  
* lambda\_home ≥ 2.2 OR lambda\_away ≥ 2.2  
* Missing or unreliable data

These filters exist to eliminate dominance and chaos games.

---

## **✅ DECISION RULES**

### **🔴 PRIMARY MARKET —** 

### **R3-NO**

Flag **R3-NO** ONLY if **ALL** conditions are met:

* P\_R3\_NO ≥ 0.75  
* 0.9 ≤ lambda\_home ≤ 1.8  
* 0.9 ≤ lambda\_away ≤ 1.8  
* 0.35 ≤ p\_home ≤ 0.65  
* Odds (if provided): ≥ 1.60 (do NOT fetch odds automatically)

### **🟢 SECONDARY MARKET —** 

### **R3-YES**

###  **(RARE)**

Flag **R3-YES** ONLY if **ALL** are true:

* P\_R3\_YES ≥ 0.30  
* One team dominance:  
  * p ≥ 0.65  
  * lambda ≥ 2.2  
* lambda\_home \+ lambda\_away ≥ 2.8  
* Odds (if provided): ≥ 2.80

Expect **very few** R3-YES flags.

## **⏭ SKIP LOGIC**

If neither R3-NO nor R3-YES conditions are met:

→ **Decision \= SKIP**

SKIP is a valid and expected outcome.

---

## **📤 OUTPUT FORMAT (REQUIRED)**

For each match, output JSON with:

{

  "fixture\_id": "...",

  "league": "...",

  "home\_team": "...",

  "away\_team": "...",

  "lambda\_home": 1.23,

  "lambda\_away": 1.11,

  "p\_home": 0.52,

  "p\_away": 0.48,

  "P\_R3\_YES": 0.21,

  "P\_R3\_NO": 0.79,

  "passes\_filters": true,

  "decision": "R3-NO",

  "rejection\_reasons": \[\]

}

## **▶️ EXECUTION**

main\_run3.py must:

1. Fetch **ALL football fixtures worldwide for the given date**  
2. Compute lambdas (from shared data)  
3. Apply Run-3 probability logic  
4. Apply filters  
5. Assign decision (R3-NO / R3-YES / SKIP)  
6. Output:  
   * Terminal summary  
   * JSON file (run3\_output\_YYYY-MM-DD.json)

Accept zero-bet days gracefully.

---

## **🧠 PHILOSOPHY (DO NOT IGNORE)**

* This market is **about balance and sequence disruption**  
* Frequency is NOT a goal  
* Accuracy \> action  
* NO optimization for “more bets”  
* NO blending with GG logic

---

## **✅ FINAL CONFIRMATION**

Before finishing, ensure:

* GG code untouched  
* Run-3 logic isolated  
* Math implemented exactly as specified  
* Filters enforced strictly  
* All leagues included  
* Output explainable

