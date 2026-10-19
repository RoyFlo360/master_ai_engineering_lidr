"""Security: traversal, unsafe YAML, input caps, error bodies, PII plaintext.

The threat model is modest but real: the catalog is a file other people edit,
the endpoint is unauthenticated (documented residual risk), and the mapping
store is the one place where a mistake would put personal data at rest.

The ``env`` and ``make_client`` fixtures come from ``test_ingestion_api`` — one
fixture, two concerns, so the two modules cannot drift apart.
"""
from __future__ import annotations

import hashlib
import hmac
import uuid
from pathlib import Path

import pytest
import yaml

from app.ingestion.catalog.loader import load_catalog
from app.ingestion.loaders.filesystem import FileSystemLoader
from app.ingestion.pii import ConsistentPseudonymizer, InMemoryMappingStore, PIISpan
from tests.test_ingestion_api import env, make_client  # noqa: F401 - fixtures

SALT = "test-salt"


class RecordingStore(InMemoryMappingStore):
    """In-memory store that records the keys it is asked to create."""

    def __init__(self) -> None:
        super().__init__()
        self.seen: list[tuple[str, str]] = []

    def lookup_or_create(self, entity_type, original_hash, new_pseudonym_factory):
        self.seen.append((entity_type, original_hash))
        return super().lookup_or_create(entity_type, original_hash, new_pseudonym_factory)


class StubAnalyzer:
    def __init__(self, spans):
        self._spans = spans

    def analyze(self, text, language="es", entities=None):
        return list(self._spans)


# --------------------------------------------------------------------------- #
# Filesystem boundary
# --------------------------------------------------------------------------- #
def test_loader_refuses_traversal_out_of_the_data_root(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "leak.json").write_text("{}", encoding="utf-8")

    loader = FileSystemLoader(root)

    for location in ("../outside", "../../etc", "budgets/../../outside"):
        with pytest.raises(ValueError, match="escapes the configured data root"):
            list(loader.iter_blobs(location, {"json"}))


def test_loader_rejects_an_absolute_path_anywhere(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()

    with pytest.raises(ValueError, match="escapes the configured data root"):
        FileSystemLoader(root).resolve(str(Path(tmp_path).resolve()))


def test_traversal_error_does_not_echo_the_host_path(tmp_path):
    root = tmp_path / "seed"
    root.mkdir()

    with pytest.raises(ValueError) as excinfo:
        FileSystemLoader(root).resolve("../../etc/passwd")

    message = str(excinfo.value)
    assert "etc/passwd" not in message or "../../etc/passwd" in message
    assert str(tmp_path) not in message


# --------------------------------------------------------------------------- #
# YAML
# --------------------------------------------------------------------------- #
def test_catalog_loader_uses_safe_load_and_executes_nothing(tmp_path):
    marker = tmp_path / "pwned.txt"
    payload = (
        f"!!python/object/apply:os.system ['echo pwned > {marker.as_posix()}']\n"
    )
    path = tmp_path / "catalog.yaml"
    path.write_text(payload, encoding="utf-8")

    with pytest.raises(yaml.YAMLError):
        load_catalog(path)

    assert not marker.exists(), "yaml.safe_load must never execute a tag"


def test_catalog_loader_rejects_a_document_that_is_not_a_mapping(tmp_path):
    path = tmp_path / "catalog.yaml"
    path.write_text("just a string\n", encoding="utf-8")

    with pytest.raises(ValueError):
        load_catalog(path)


# --------------------------------------------------------------------------- #
# HTTP input caps and error bodies
# --------------------------------------------------------------------------- #
def test_oversized_source_name_is_422(env):  # noqa: F811 - pytest fixture
    with make_client(env) as client:
        response = client.post(
            "/api/v1/ingestion/runs", json={"source_name": "x" * 5000}
        )

    assert response.status_code == 422


def test_extra_fields_in_the_body_are_rejected(env):  # noqa: F811
    with make_client(env) as client:
        response = client.post(
            "/api/v1/ingestion/runs",
            json={"source_name": "presupuestos_json", "decision": "include"},
        )

    assert response.status_code == 422


def test_error_bodies_leak_no_filesystem_paths(env):  # noqa: F811
    with make_client(env) as client:
        unknown = client.post("/api/v1/ingestion/runs", json={"source_name": "no_existe"})
        excluded = client.post("/api/v1/ingestion/runs", json={"source_name": "rate_card_xlsx"})
        missing_job = client.get(f"/api/v1/ingestion/jobs/{uuid.uuid4()}")

    for response in (unknown, excluded, missing_job):
        body = response.text
        assert str(env.seed) not in body
        assert "sqlite" not in body.lower()
        assert "Traceback" not in body

    assert set(unknown.json()) == {"reason", "source_name"}
    assert set(missing_job.json()) == {"reason"}


def test_background_failure_is_contained_and_bounded(env):  # noqa: F811
    """A failing run must not 500 the request, and must not store a huge blob."""

    class ExplodingRegistry:
        def get(self, fmt):
            raise RuntimeError("internal failure " + "y" * 5000)

        def formats(self):
            return {"json", "txt"}

    with make_client(env, registry=ExplodingRegistry()) as client:
        posted = client.post("/api/v1/ingestion/runs", json={"source_name": "presupuestos_json"})
        job = client.get(f"/api/v1/ingestion/jobs/{posted.json()['job_id']}").json()

    assert posted.status_code == 202
    assert job["status"] == "failed"
    assert len(job["error_message"]) <= 2048


# --------------------------------------------------------------------------- #
# The mapping store never sees plaintext
# --------------------------------------------------------------------------- #
def test_mapping_store_receives_only_hmac_digests():
    text = "Laura Fernández, laura.fernandez@acme.es"
    spans = [
        PIISpan("PERSON", 0, len("Laura Fernández")),
        PIISpan(
            "EMAIL_ADDRESS",
            text.index("laura.fernandez@acme.es"),
            len(text),
        ),
    ]
    store = RecordingStore()
    pseudonymizer = ConsistentPseudonymizer(
        analyzer=StubAnalyzer(spans), mapping_store=store, salt=SALT
    )

    result = pseudonymizer.pseudonymize(text)

    assert result.pseudonymized_text != text
    assert len(store.seen) == 2
    expected_person = hmac.new(SALT.encode(), "Laura Fernández".encode(), hashlib.sha256).hexdigest()
    for entity_type, digest in store.seen:
        assert entity_type in {"PERSON", "EMAIL_ADDRESS"}
        assert len(digest) == 64
        assert digest.isalnum()
        assert "Laura" not in digest
        assert "acme" not in digest
        int(digest, 16)  # valid hex
    # Spans are applied right-to-left, so the store records the email first;
    # the assertion is about *what* is stored, not the order it arrived in.
    assert ("PERSON", expected_person) in store.seen


def test_pseudonymizer_hashes_are_salted():
    """The same value under two salts must not collide in the store."""
    text = "Laura Fernández"
    span = PIISpan("PERSON", 0, len(text))

    first = RecordingStore()
    second = RecordingStore()
    ConsistentPseudonymizer(
        analyzer=StubAnalyzer([span]), mapping_store=first, salt="salt-one"
    ).pseudonymize(text)
    ConsistentPseudonymizer(
        analyzer=StubAnalyzer([span]), mapping_store=second, salt="salt-two"
    ).pseudonymize(text)

    assert first.seen[0][1] != second.seen[0][1]


def test_pseudonymized_text_does_not_contain_the_original_identifier():
    text = "Presupuesto BUDGET-2024-0001 del cliente CLI-0042"
    from app.ingestion.pii import RegexFallbackAnalyzer

    store = InMemoryMappingStore()
    pseudonymizer = ConsistentPseudonymizer(
        analyzer=RegexFallbackAnalyzer(), mapping_store=store, salt=SALT
    )

    result = pseudonymizer.pseudonymize(text)

    assert "BUDGET-2024-0001" not in result.pseudonymized_text
    assert "CLI-0042" not in result.pseudonymized_text
    assert len(result.applied) == 2
