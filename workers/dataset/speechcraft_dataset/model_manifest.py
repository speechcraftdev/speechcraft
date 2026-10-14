"""Compatibility imports for the independent speechcraft-models package."""
from speechcraft_models.manifest import (  # noqa: F401
    ManifestError, SCHEMA_PATH, SUPPORTED_PLATFORMS, load_catalog, main,
    parse_manifest, select_artifact, validate_artifact, validate_manifest, verify_artifact,
)
# Preserve the old mock target used by the worker's verifier regression suite.
from speechcraft_models.manifest import os  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
