from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve .env from the project root so settings load the same way regardless of
# the current working directory the server is started from.
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# DeepSeek is the default provider. The model id lives here once so the raw DeepSeek
# name (LLM_MODEL, which llm_service sends to the DeepSeek API) and the LiteLLM form
# (PRIMARY_MODEL) cannot drift apart.
DEEPSEEK_MODEL = "deepseek-flash"


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(env_file=ENV_FILE, env_file_encoding="utf-8")
    #session 2
    OPENAI_API_KEY: str | None = None
    ANTHROPIC_API_KEY: str | None = None
    DEEPSEEK_API_KEY: str | None = None
    LLM_PROVIDER: Literal["deepseek", "anthropic"] = "deepseek"
    LLM_MODEL: str = DEEPSEEK_MODEL
    APP_ENV: Literal["development", "staging", "production"] = "development"
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "DEBUG"
    #session 3
    PRIMARY_MODEL: str = f"deepseek/{DEEPSEEK_MODEL}"
    FALLBACK_MODEL: str = "claude-haiku-4-5-20251001"
    LLM_TIMEOUT: int = 30
    LLM_RETRIES: int = 2
    # One source of truth for the output cap: the request may override it per call.
    MAX_OUTPUT_TOKENS: int = 8000
    # DeepSeek runs in thinking mode by default and bills the chain of thought as
    # output tokens (measured: 4363 billed against 1812 characters of answer).
    # Off by default to keep estimations cheap; set true to get the reasoning back.
    ENABLE_THINKING: bool = False
    REDIS_URL: str = "redis://localhost:6379"
    CACHE_TTL: int = 86400
    # Session 5 — conversational sessions, attachments, guardrails, semantic cache.
    # Per-attachment cap on extracted text. 60_000 so the stress corpus's 50 KB
    # point sits just under the cap and its 100 KB point triggers truncation.
    MAX_ATTACHMENT_CHARS: int = 60_000
    MAX_CONVERSATION_TURNS: int = 6
    METADATA_EXTRACTOR_MODEL: str = "gpt-4o-mini"
    # Metadata is refreshed heuristically; the LLM extractor only runs when the
    # heuristic finds no project name/team/technology for the turn.
    METADATA_LLM_FALLBACK: bool = True
    # Session 6 — hybrid memory compression, dynamic tier, Actor-Critic-Boss.
    # Anchor detector: "heuristic" (regex over key phrases) or "llm" (binary
    # classifier via Instructor). Heuristic is the default for cost.
    ANCHOR_DETECTION_MODE: Literal["heuristic", "llm"] = "heuristic"
    # Cheap model used by the cumulative summarizer (history compression).
    COMPRESSION_MODEL: str = "gpt-4o-mini"
    # Conversational prompt version used by ``estimate_conversational``.
    # v2 = pre-live-session baseline. v3 = adds <audience> driven by tier and an
    # optional <critic_feedback> block consumed by the Boss.
    CONVERSATIONAL_PROMPT_VERSION: str = "v3"
    # Critic model (read-only auditor; cheap is fine).
    CRITIC_MODEL: str = "gpt-4o-mini"
    # Fallback for the auxiliary calls (metadata extractor, summarizer, critic,
    # anchor classifier). Those default to an OpenAI model; if that provider is
    # unavailable (no credits / quota), retry the call once on this model.
    # Empty string disables the fallback.
    AUX_FALLBACK_MODEL: str | None = "deepseek/deepseek-flash"
    # Max iterations the Boss may drive (each = 1 actor + 1 critic call).
    # Three is the practical floor: one initial draft + two directed retries.
    BOSS_MAX_ITERATIONS: int = 3
    EMBEDDING_MODEL: str = "text-embedding-3-small"
    SEMANTIC_CACHE_THRESHOLD: float = 0.92
    SEMANTIC_CACHE_TTL: int = 86400
    SEMANTIC_CACHE_LOG_ONLY: bool = False

    # Session 10 — hybrid retrieval (full-text + vector, fused with RRF) and
    # cross-encoder reranking. These are the knobs the four evaluation
    # configurations (vector/hybrid x rerank on/off) are built from; every one of
    # them must be changeable without editing code.
    #
    # Note: the full-text configuration is NOT here. It lives beside the SQL that
    # uses it (`FTS_REGCONFIG` in `app/embedding_pipeline/store/repository.py`)
    # and must match the migration's `to_tsvector(...)` literal exactly. Making it
    # a setting would let an .env value silently disagree with the generated
    # column, which bypasses the GIN index without failing anything.
    # Default retrieval mode. "vector" reproduces the pre-Session-10 behaviour.
    RETRIEVAL_SEARCH_MODE: Literal["vector", "hybrid"] = "vector"
    # RRF smoothing constant (Cormack et al. default). Larger = a chunk must rank
    # well in BOTH branches to win; smaller = a single #1 can dominate.
    RRF_K: int = Field(default=60, ge=1, le=1_000)
    # Recall-then-rerank width: fetch this many candidates, then let the
    # cross-encoder cut them down to RERANK_TOP_N. This is the expensive knob —
    # it drives both the SQL LIMIT and the cross-encoder batch size.
    RETRIEVAL_RECALL_TOP_K: int = Field(default=50, ge=1, le=200)
    RERANK_TOP_N: int = Field(default=5, ge=1, le=50)
    # Whether reranking runs when the request does not say. Off by default: the
    # model is a ~120 MB download and the cheap path must stay cheap.
    RERANKER_ENABLED: bool = False
    # Multilingual cross-encoder (ES+EN), small enough to run on CPU.
    RERANKER_MODEL: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    # Cosine-distance floor for the grounded-estimation chain. `/v1/retrieval/search`
    # deliberately does not apply a floor by default (a caller wants to see the
    # nearest chunks even when they are poor); the chain does, because "the corpus
    # has no analogue for this project" is a result it must be able to report.
    # Calibrated on this corpus on 2026-11-08: relevant queries land at 0.43–0.60,
    # an unrelated topic ("satellite imagery of coral reefs") at 0.71–0.72. Re-tune
    # if the embedding model or the chunking template changes.
    RETRIEVAL_DISTANCE_THRESHOLD: float = Field(default=0.65, ge=0.0, le=2.0)

    # Session 7 (extension) — data ingestion: catalog, cleaning, PII, persistence.
    # Paths are resolved from the project root, never from the process working
    # directory: the container runs from /srv/app and a host run may not.
    # DATABASE_URL defaults to SQLite so the test suite and a laptop run need no
    # database; docker-compose overrides it with Postgres for the heavier runs.
    DATABASE_URL: str = (
        f"sqlite:///{(Path(__file__).resolve().parent.parent / 'var' / 'estimator.db').as_posix()}"
    )
    # Where the YAML catalog lives.
    CATALOG_PATH: Path = Path(__file__).resolve().parent.parent / "data" / "catalog" / "catalog.yaml"
    # Root that ``CatalogSource.location`` entries are resolved against.
    INGESTION_DATA_ROOT: Path = Path(__file__).resolve().parent.parent / "data" / "seed"
    # Presidio/spaCy model used for Spanish PII detection.
    PRESIDIO_SPACY_MODEL: str = "es_core_news_md"
    # PII analyzer: "auto" uses Presidio when the spaCy model is installed and
    # degrades to the regex fallback otherwise; "presidio"/"regex" force one.
    PII_ANALYZER_MODE: Literal["auto", "presidio", "regex"] = "auto"
    # Locale used by Faker to generate consistent pseudonyms.
    PSEUDONYM_FAKER_LOCALE: str = "es_ES"
    # HMAC salt for the pseudonym mapping. Rotating it invalidates every prior
    # mapping — that is the GDPR Art. 17 lever. CHANGE IN PRODUCTION.
    PSEUDONYM_HASH_SALT: str = "change-me-in-prod"

    @property
    def llm_api_key(self) -> str | None:
        """Credential for the configured provider.

        DeepSeek is consumed through the OpenAI-compatible API, so the key may be
        provided as DEEPSEEK_API_KEY or, for backwards compatibility, in the
        OPENAI_API_KEY slot.
        """
        if self.LLM_PROVIDER == "deepseek":
            return self.DEEPSEEK_API_KEY or self.OPENAI_API_KEY
        if self.LLM_PROVIDER == "anthropic":
            return self.ANTHROPIC_API_KEY
        return None

    @model_validator(mode="after")
    def validate_api_key_for_provider(self) -> "Settings":
        """Ensure the API key for the selected LLM provider is present."""
        if not self.llm_api_key:
            raise ValueError(
                f"No API key configured for LLM_PROVIDER={self.LLM_PROVIDER!r}. "
                "Set DEEPSEEK_API_KEY (or OPENAI_API_KEY) for 'deepseek', "
                "or ANTHROPIC_API_KEY for 'anthropic'."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return cached application settings (singleton)."""
    return Settings()
