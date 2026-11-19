# Router accuracy

Model `claude-haiku-5-5` · 47 cases × 3 repeat(s) = 141 calls.

- **Accuracy:** 97.9% (a route the case accepts); strict 97.9% (the intended route).
- **Consistency:** 100.0% of cases got the same route on every repeat.
- **Fallbacks** (no valid route, became clarify): 0 · **errors:** 0.
- **Latency:** median 1.8 s, p95 2.42 s, max 4.52 s.
- **Tokens:** 564 in (127864 from cache), 26750 out.

## By intended route

| Intended | Calls | Accuracy | Got |
|---|---|---|---|
| architect | 54 | 94.4% | architect 51, requirements 3 |
| clarify | 15 | 100.0% | clarify 15 |
| requirements | 54 | 100.0% | requirements 54 |
| requirements_then_architect | 18 | 100.0% | requirements_then_architect 18 |

## Misses (3)

| Case | Repeat | Expected | Got | Message | Router's reason |
|---|---|---|---|---|---|
| h-arch-08 | 1 | architect | requirements | Can discount_amount be summed across orders, or only within one? | The message asks what discount_amount means in business terms, specifically whether it can be summed across orders or only within a single order. |
| h-arch-08 | 2 | architect | requirements | Can discount_amount be summed across orders, or only within one? | You're asking what the discount_amount measure means, specifically whether it can be totaled across orders or only within a single order. |
| h-arch-08 | 3 | architect | requirements | Can discount_amount be summed across orders, or only within one? | You're asking what discount_amount means in business terms, specifically whether it can be summed across orders or only within a single order. |
