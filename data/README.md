# `data/` — corpus y catálogo de fuentes

Este árbol alimenta el subsistema de ingesta (`app/ingestion/`, spec
`011-session-7-data-ingestion.md`). Nada de lo que hay aquí lo genera el
servicio en tiempo de ejecución: es corpus de entrada, versionado en git.

```
data/
  budgets_sample.json          # corpus de embeddings (spec 010) — otro esquema, no mezclar
  catalog/catalog.yaml         # catálogo auditado de fuentes (decisión + motivo)
  seed/budgets/*.json          # 6 presupuestos firmados 2024 (esquema de ingesta)
  seed/transcripts/*.txt       # 3 transcripciones: 2 con tags, 1 heredada sin tags
  seed/rate_card_2024.xlsx     # tarifa interna — fuente `exclude` del catálogo
```

## Dos esquemas de presupuesto que **no** son intercambiables

- `data/budgets_sample.json` (Track B / embeddings): `budget_id`,
  `client_metadata`, `project_summary`, `main_technology`, `year`,
  `total_estimated_hours`, `components[]`.
- `data/seed/budgets/*.json` (Track A / ingesta): `budget_id`, `client_name`,
  `client_code`, `currency`, `total_amount`, `signed_at`, `phases[]`,
  `contact`, `contact_email`, `notes`.

Pasar uno al parser del otro falla en validación. Normalizar el corpus firmado
al esquema de embeddings está fuera de alcance (fase de retrieval).

## Procedencia

Copiado del material del curso de referencia del módulo (proyecto
`estimator`, rama `session_07`, fase «Sesión 6 — ingesta de datos»), con dos
cambios: este README y las notas de atribución. Los datos son sintéticos: los
nombres, correos e importes no corresponden a ningún cliente real.

El corpus está diseñado para provocar las cinco familias de suciedad que la
limpieza debe tratar: `TBD`/`N/A` como nulos disfrazados, `eur` en minúsculas,
fechas `dd/mm/aaaa`, importes negativos (nota de abono) y un par de ficheros
duplicados (`BUDGET-2024-0005-v1`/`-v2`) que el dedup debe colapsar.
