#!/usr/bin/env python3
"""Demo — pseudonymization over a real transcript, with an in-memory mapping.

Builds the analyzer the same way the pipeline does (``build_pseudonymizer``):
Presidio + ``es_core_news_md`` when the model is installed, the regex fallback
otherwise — and it says which one it used, because that difference is the whole
teaching point (the fallback cannot find person names).

The mapping store is ``InMemoryMappingStore`` on purpose: a demo must not depend
on a migrated database. Nothing but HMACs reaches the store, so printing the
pseudonymized text is safe; printing the original text is what the corpus file
is for.

Usage::

    .venv\\Scripts\\python.exe scripts/demo_pii.py
    .venv\\Scripts\\python.exe scripts/demo_pii.py data/seed/transcripts/transcripcion_2025-01-15_acme.txt
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Make ``app`` importable when this script runs directly from ``scripts/``.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# A salt is required to build the pseudonymizer; use a demo one when unset so the
# script runs on a bare checkout. Never reuse this value outside the demo.
os.environ.setdefault("PSEUDONYM_HASH_SALT", "demo-salt")

from app.config import Settings  # noqa: E402
from app.ingestion.pii import (  # noqa: E402
    ConsistentPseudonymizer,
    InMemoryMappingStore,
    PIIAnalyzerUnavailable,
    build_analyzer,
    build_pseudonymizer,
)

DEFAULT_TRANSCRIPT = (
    ROOT / "data" / "seed" / "transcripts" / "transcripcion_2025-02-03_betanorte.txt"
)


def _build(settings: Settings, store: InMemoryMappingStore) -> ConsistentPseudonymizer:
    """Presidio when possible, regex fallback otherwise — never crash a demo."""
    try:
        analyzer = build_analyzer(settings.PRESIDIO_SPACY_MODEL)
        print(f"Analyzer: Presidio + {settings.PRESIDIO_SPACY_MODEL}")
        return ConsistentPseudonymizer(
            analyzer=analyzer,
            mapping_store=store,
            salt=settings.PSEUDONYM_HASH_SALT,
            faker_locale=settings.PSEUDONYM_FAKER_LOCALE,
        )
    except PIIAnalyzerUnavailable as exc:
        print(f"{exc}\nAnalyzer: regex fallback (no person names — that limit is the point)")
        return build_pseudonymizer(
            settings, store, mode="regex"
        )


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    transcript = Path(args[0]) if args else DEFAULT_TRANSCRIPT
    if not transcript.exists():
        print(f"ERROR: no existe {transcript}")
        return 1

    settings = Settings()
    store = InMemoryMappingStore()
    pseudonymizer = _build(settings, store)

    original = transcript.read_text(encoding="utf-8")
    result = pseudonymizer.pseudonymize(original)

    print(f"\nTranscripción: {transcript.name} ({len(original)} chars)")
    print("\n--- Texto pseudonimizado (primeros 400 chars) ---")
    print(result.pseudonymized_text[:400])

    print(f"\n--- Mappings aplicados ({len(result.applied)}) ---")
    for mapping in result.applied[:12]:
        print(f"  {mapping.entity_type:14} -> {mapping.pseudonym}")

    print(f"\nMappings en el store: {len(store)}  "
          "(clave = HMAC del valor, nunca el valor)")

    # Consistency check: the same text twice must produce the same pseudonyms.
    again = pseudonymizer.pseudonymize(original)
    same = again.pseudonymized_text == result.pseudonymized_text
    print(f"Consistencia entre llamadas: {'OK' if same else 'FALLO'}")
    return 0 if same else 2


if __name__ == "__main__":
    raise SystemExit(main())
