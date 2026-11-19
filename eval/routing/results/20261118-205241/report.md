# Request router accuracy

Model `claude-haiku-5-5` · 50 cases × 3 repeat(s) = 150 calls.

- **Accuracy:** 99.3% (a route the case accepts); strict 99.3% (the intended route).
- **Consistency:** 98.0% of cases got the same route on every repeat.
- **Fallbacks** (no valid route, became clarify): 0 · **errors:** 0.
- **Latency:** median 1.17 s, p95 2.12 s, max 2.91 s.
- **Tokens:** 600 in (135870 from cache), 31071 out.

## By intended route

| Intended | Calls | Accuracy | Got |
|---|---|---|---|
| architect | 54 | 100.0% | architect 54 |
| clarify | 18 | 100.0% | clarify 18 |
| requirements | 48 | 100.0% | requirements 48 |
| requirements_then_architect | 30 | 96.7% | requirements_then_architect 29, architect 1 |

## Misses (1)

| Case | Repeat | Expected | Got | Message | Router's reason |
|---|---|---|---|---|---|
| both-09 | 2 | requirements_then_architect | architect | Add a region attribute to stores so finance can roll up sales by region. | The message asks for a design change: add a region attribute to the stores dimension. The finance roll-up is the reason given, not a new requirement stated on its own. |
