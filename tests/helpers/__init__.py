"""
Shared test helpers.

A real package, so that each file in here has exactly ONE module name. mypy
computes a module name by walking up while it keeps finding `__init__.py`: it
stops at `tests/` (which deliberately has none), giving `helpers.settlement_
fixtures` - precisely the name the importing tests use.

`tests/__init__.py` is deliberately ABSENT and must stay absent. Adding one
makes pytest resolve the shared root conftest as `tests.conftest`, which breaks
the three existing files that do `from conftest import espn_event, utc`.
"""
