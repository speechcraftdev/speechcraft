from __future__ import annotations

import unittest

from app.setup_health import matches_setup_health_identity


class SetupHealthIdentityTests(unittest.TestCase):
    def test_private_setup_health_requires_the_current_launch_identity(self) -> None:
        identity = "a" * 64
        self.assertTrue(matches_setup_health_identity(identity, identity))
        self.assertFalse(matches_setup_health_identity(identity, None))
        self.assertFalse(matches_setup_health_identity(identity, "b" * 64))

    def test_private_setup_health_is_closed_when_launch_identity_is_unset(self) -> None:
        self.assertFalse(matches_setup_health_identity(None, "a" * 64))
