# Technologies

Everything below is declared in `pyproject.toml`. The service targets
`requires-python = ">=3.14"`; the image is `python:3.14-slim` and dependencies are
installed with `uv sync --frozen --no-install-project`. `uv.lock` is the resolved
lockfile; `uv lock --check` reports whether it still matches the project file.

## Runtime dependencies

| Dependency | Constraint | Role | What breaks without it |
| --- | --- | --- | --- |
| `fastapi` | `>=0.141.1` | HTTP app, routing, request validation, OpenAPI | No HTTP layer; every endpoint disappears |
| `uvicorn[standard]` | `>=0.53.0` | ASGI server (`uvicorn app.main:app`) | The app cannot be served |
| `sse-starlette` | `>=3.4.11` | `EventSourceResponse` for the streaming route | `/api/v1/estimate/stream` cannot stream |
| `python-multipart` | `>=0.0.9` | Parses `multipart/form-data` on the session endpoints | `Form(...)`/`File(...)` endpoints fail at startup |
| `pydantic-settings` | `>=2.15.0` | `Settings`, `.env` loading, provider-key validation | `app/config.py` does not import; nothing starts |
| `python-dotenv` | `>=1.2.3` | Reads the root `.env` file for `Settings` | `.env` values are ignored |
| `litellm` | `>=1.83.0` | `Router` with primary/fallback deployments; provider-agnostic completion | `LLMWrapper.complete`, `complete_stream` and every Instructor call fail |
| `instructor` | unpinned | Structured output: validates the model against a Pydantic schema and re-prompts | Typed/conversational/ACB flows cannot produce `EstimationResult`, `CriticFeedback` or `ProjectMetadata` |
| `openai` | `>=1.0.0,<3.13` | SDK used directly by the legacy flow, the moderation client and LiteLLM's OpenAI-compatible calls | `/api/v1/estimate` fails; moderation and the semantic cache vectorizer are unavailable |
| `jinja2` | `>=3.1.6` | Renders the versioned prompt templates | `app/prompts/loader.py` fails; all prompt-rendered flows fail |
| `redis` | `>=8.1.0` | Exact-match cache client; RediSearch module probe | `EstimationCache.from_url` fails; caching degrades (the app still starts) |
| `redisvl` | `>=0.27.2` | Vector index and query for the semantic cache | The semantic cache cannot be built; it stays a no-op |
| `pypdf` | `>=6.19.0` | Extracts text from `.pdf` attachments | PDF attachments raise an extraction error (422) |
| `python-docx` | `>=1.2.0` | Extracts text from `.docx` attachments | DOCX attachments raise an extraction error (422) |
| `fpdf2` | `>=2.7.9` | Builds the stress fixture PDFs (`evals/stress/fixtures/build_pdfs.py`) | The stress size curve cannot be generated |
| `structlog` | `>=26.1.0` | Structured logging; JSON in production, console otherwise | `app/main.py` and every module that logs fail to import |
| `streamlit` | `>=1.64.0` | The three HTTP clients; compose runs `streamlit_app_loader.py` | The UI container has nothing to run |
| `tiktoken` | `>=0.7.0` | Token counting for the embedding chunker (`app/embedding_pipeline/chunker.py`) | `token_count` is unavailable; the embedder cannot size its batches by tokens |
| `pyyaml` | `>=6.0` | Catalog loading (`app/ingestion/catalog/loader.py`) | The catalog cannot be read; ingestion has no allow-list |
| `sqlalchemy[asyncio]` | `>=2.0` | Persistence models and repositories, plus the async RAG store. The extra pulls `greenlet`, which SQLAlchemy's asyncio extension requires | No job rows, no pseudonym mappings, and no async vector store |
| `asyncpg` | `>=0.29` | Async Postgres driver for `/embeddings/ingest` and `/search` (spec 012) | The vector endpoints cannot run; the sync stack is unaffected |
| `pgvector` | `>=0.3` | Registers the `vector` column type and exposes `cosine_distance` to SQLAlchemy | The RAG tables cannot be declared or migrated |
| `anthropic` | `>=0.40` | Native Anthropic SDK, used by the `contextual_retrieval` chunker for `cache_control` on the parent document | That strategy is unavailable; the comparison still runs the other seven |
| `langchain-text-splitters` | `>=0.3.0` | Separator-hierarchy splitting for the `recursive` chunker | The `recursive` strategy is unavailable |
| `langchain-experimental` | `>=0.3.0` | `SemanticChunker` (breakpoint splitting) for the `semantic` strategy. **Upstream marks it sunset** | The `semantic` strategy is unavailable |
| `langchain-openai` | `>=0.2.0` | `OpenAIEmbeddings` adapter the semantic splitter needs | Same as above |
| `nltk` | `>=3.9` | Sentence tokenization for the `sentence_window` chunker; downloads `punkt_tab` lazily | That strategy raises an actionable error until the data is present |
| `alembic` | `>=1.13` | Schema migrations (`alembic/`) | The tables cannot be created; compose's `upgrade head` fails |
| `psycopg[binary]` | `>=3.1` | Postgres driver | `DATABASE_URL` must stay on SQLite |
| `pandas` | `>=2.2` | Cleaning of raw budget records | Ingestion cannot clean; every parsed record becomes a document |
| `pandera` | `>=0.20` | Row validation and quarantine routing | No valid/quarantined/discarded partition |
| `openpyxl` | `>=3.1` | Reads the XLSX seed source | The (excluded) rate-card source could not be inspected |
| `faker` | `>=24.0` | Consistent pseudonyms | Pseudonym generation fails |
| `presidio-analyzer` | `>=2.2` | PII detection | Only the regex fallback remains (no person names) |
| `presidio-anonymizer` | `>=2.2` | PII replacement | Same as above |
| `spacy` | `>=3.7` | Spanish NER model behind Presidio | Only the regex fallback remains until the model is installed |
| `es-core-news-md` | pinned wheel URL, `3.8.0` | Spanish NER model (`PRESIDIO_SPACY_MODEL`) | Presidio misses Hispanic names; PII degrades to emails/ids only — no `PERSON` |

## Development dependencies

| Dependency | Constraint | Role |
| --- | --- | --- |
| `pytest` | `>=9.1.1` | Test runner; `testpaths = ["tests"]`, `pythonpath = ["."]` |
| `httpx` | `>=0.28.1` | Transport under `fastapi.testclient.TestClient` and the evals `--http` mode |

`pytest` is configured in `pyproject.toml` with `addopts = "-ra"`; there is no lint
or type-check step in the repository.

## Declared only transitively

Two modules are imported directly but are not declared in `pyproject.toml`:

- `numpy`, imported by `app/services/semantic_cache.py` to convert vectors to
  float32 bytes. It arrives through `redisvl`/`pandas`.
- `pydantic`, imported by every schema module. It arrives through `fastapi`.

They work today because their parents pin them. A future dependency change could
remove either without warning, so a change that touches them should consider
declaring them.

## The three token caps

There is no single output cap. Three sites decide, and they do not agree:

| Site | Value | Applies to |
| --- | --- | --- |
| `app/services/llm_service.py` module constant | 8000 | Legacy `POST /api/v1/estimate` |
| `LLMWrapper.complete(..., max_tokens=4000)` signature default | 4000 | `POST /api/v1/new-estimate` (calls without `max_tokens`), and the conversational/ACB actor calls through `complete_structured_chat` |
| `Settings.MAX_OUTPUT_TOKENS` | 8000 (default) | `POST /api/v1/estimate/stream` when the request omits `max_tokens`; stored on the wrapper as `wrapper.max_tokens` |

Two further caps stack on top: `StreamEstimationRequest.max_tokens` accepts 1–16000,
and the auxiliary calls pass their own small values (critic 1500, summarizer 1000,
metadata extractor 1000, anchor classifier 200). Changing `MAX_OUTPUT_TOKENS` does not
move the legacy flow or `/new-estimate`.

## Pricing tables and roundings

Two independent tables price the same product:

| Table | Location | Models | Used by | Rounding |
| --- | --- | --- | --- | --- |
| `PRICING` (local dict in `cost_llm`) | `app/services/llm_service.py` | `deepseek-flash` only | Legacy `/api/v1/estimate` | `round(cost, 4)` |
| `MODEL_COSTS` (module dict) | `app/services/llm_wrapper.py` | `deepseek-flash`, `gpt-4o-mini`, `gpt-4o`, `claude-haiku-4-5`, `claude-haiku-4-5-20251001`, `claude-sonnet-4-5` | Streaming, `/new-estimate`, per-turn observations | `round(cost, 6)` |

Both agree on DeepSeek's rates (0.15 input / 0.60 output per million tokens). They
differ in coverage and precision: the same call prices as `0.0008` on the legacy flow
and `0.000797` on the others. The 6-decimal rounding is deliberate — at 4 decimals any
call cheaper than about $0.00005 reads as `$0.0000` and disappears from totals. The
ledger sums 6-decimal values and rounds the total to 6 again.

A model missing from `MODEL_COSTS` is priced at zero and logged as
`model_not_in_cost_table`; the table is not a fallback estimate.

Display precision belongs in the client. `format_cost` in `streamlit_app_loader.py`
formats for the UI; the pricing modules stay numeric.

## Deliberately absent

- `unstructured`. The project does not parse arbitrary documents. Attachments are
  `.pdf` and `.docx` only, through `pypdf` and `python-docx`, and the planned
  ingestion parsers are JSON and TXT only. Adding `unstructured` would pull a large
  dependency tree for formats the catalog does not include.
- `numpy`/`scikit-learn` as declared dependencies. Cosine similarity in the planned
  embedding script is computed with `math`; the only numeric use in the shipped code
  is the semantic cache's float32 conversion.
- Any web framework beyond FastAPI, any database driver in the running service, and
  any task queue. The service is one process.
