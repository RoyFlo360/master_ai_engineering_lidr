# Embedding sanity check

Model: `text-embedding-3-small` (1536 dimensions, default).
Generated with `scripts/compare.py` (cosine computed by hand, stdlib only).
Run date: 2026-10-18.

| Pair | Text A | Text B | Cosine | Expectation | Observed |
|------|--------|--------|--------|-------------|----------|
| A — semantically near | "OAuth 2.0 authentication backend with JWT tokens for fintech mobile app" | "Authorization service using JSON Web Tokens for a banking application" | **0.5957** | high (> 0.6) | just below the threshold |
| B — unrelated | "OAuth 2.0 authentication backend with JWT tokens for fintech mobile app" | "Database migration from MySQL to PostgreSQL with zero downtime" | **0.1920** | low (< 0.4) | as expected |
| C — generic | "Backend services" | "API development" | **0.5407** | no fixed expectation | deceptively close to A |

## Commentary

Pair B behaves exactly as intuition predicts: two unrelated engineering topics sit
at 0.1920, comfortably under 0.4. The embeddings clearly separate "auth for
fintech" from "database migration".

Pair A is the interesting one. At **0.5957 it lands just below the 0.6
orientative threshold**, even though both sentences describe the same concept
(token-based authn/authz for banking). They share almost no surface vocabulary
("OAuth/JWT/fintech" vs "Authorization/JSON Web Tokens/banking"), so the model
has to bridge them purely semantically and nearly does. Reporting this honestly
is more useful than rounding it up: 0.6 is a rule of thumb, not a constant.

Pair C is the real talking point. Two short, generic phrases score **0.5407** —
almost as high as the genuinely-near Pair A. Short, vague text carries little
discriminating signal, so generic-vs-generic looks deceptively "close". This is
exactly why the chunker prepends a contextual header (project, sector, year,
main technology) to every component: without it, many budget components would
collapse into the same fuzzy "backend-ish" region of the embedding space.

## Reproducing

```shell
uv run python scripts/compare.py \
  --text-a "OAuth 2.0 authentication backend with JWT tokens for fintech mobile app" \
  --text-b "Authorization service using JSON Web Tokens for a banking application"
```

Inside the container:

```shell
docker compose exec api python scripts/compare.py \
  --text-a "..." --text-b "..."
```

A rerun on the same model reproduced all three scores exactly, which is expected:
embeddings are deterministic for a fixed model and input.
