# Run-3 (Unanswered Goals) Market Module

This module analyzes the "Any team to score 3 goals consecutively" market.

**Primary focus:** R3-NO (does NOT happen)  
**Secondary focus:** R3-YES (rare)

## Usage

```bash
cd run3

# Run for today
python3 main_run3.py

# Run for specific date
python3 main_run3.py 2026-01-26
```

## Files

- `run3_probability.py` - Core probability calculations
- `run3_filters.py` - Hard filters for dominance/chaos elimination
- `run3_decision.py` - R3-NO / R3-YES / SKIP decision logic
- `main_run3.py` - Main entry point

## Output

- Terminal summary
- JSON file: `run3_output_YYYY-MM-DD.json`
