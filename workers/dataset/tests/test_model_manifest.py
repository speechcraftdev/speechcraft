"""The legacy worker import delegates to the independent package."""
import unittest
from speechcraft_dataset import model_manifest as legacy
from speechcraft_models import manifest


class CompatibilityTests(unittest.TestCase):
    def test_public_verifier_aliases(self):
        for name in ("parse_manifest", "validate_manifest", "select_artifact", "verify_artifact", "ManifestError"):
            self.assertIs(getattr(legacy, name), getattr(manifest, name))
