---
trigger: always_on
---

# Production-Grade Engineering Standards

## 1. SOLID Principles
- **Single Responsibility:** Each class/module must have one reason to change.
- **Open/Closed:** Prefer composition and interfaces over inheritance.
- **Dependency Inversion:** Use Dependency Injection (DI) for external services (databases, APIs, loggers). Never hardcode secrets or connection strings.

## 2. Production Readiness & Resilience
- **Error Handling:** Use specific exception handling. No "bare" `except: pass`.
- **Logging:** Implement structured logging at appropriate levels (INFO for flow, ERROR for failures, DEBUG for data transformations).
- **Type Safety:** 100% Type Hinting coverage is mandatory. Use `Final`, `Literal`, and `Protocol` where applicable.
- **Configuration:** Use Pydantic or environment variables for all configuration.

## 3. Performance & Scaling
- **Async First:** Prioritize `async/await` for I/O bound tasks (database queries, network requests).
- **Complexity:** Avoid nested loops; prefer vectorization or efficient data structures (sets/dicts) for lookups.
- **State:** Prefer stateless functions to simplify horizontal scaling.

## 4. Testing
- Write code that is "Testable by Design." 
- If a function is too complex to unit test, it must be broken down.