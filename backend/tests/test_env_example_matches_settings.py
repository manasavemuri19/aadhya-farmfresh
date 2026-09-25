"""AAD-OPS-010: `.env.example` used to drift from the actual code defaults
(`MIN_ORDER_PAISE=9900` in the file vs. `0` in `Settings`) — exactly the kind
of thing nobody notices until someone copies the example straight into a
real deployment. This test parses the file and the `Settings` class defaults
programmatically and fails the moment they disagree again, rather than
relying on a human to remember to keep them in sync by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import SecretStr

from app.core.config import Settings

_ENV_EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"

# Fields deliberately absent from .env.example, with the reason each one is
# not a plain "copy this into your deploy" setting:
#   - app_version isn't operator-configured at all in the normal case — its
#     default comes from Railway's auto-injected git SHA (see
#     `_default_app_version` in config.py), not something you'd set by hand
#     in an env file.
_EXCLUDED_FROM_ENV_EXAMPLE = frozenset({"app_version"})


def _parse_env_example() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, rest = stripped.partition("=")
        # Strip a trailing inline comment (e.g. "ENV=local   # local | ...").
        value = re.split(r"\s+#", rest, maxsplit=1)[0].strip()
        values[key.strip()] = value
    return values


def _field_default_as_env_string(name: str) -> str:
    default = Settings.model_fields[name].default
    if isinstance(default, SecretStr):
        default = default.get_secret_value()
    if isinstance(default, bool):
        return "true" if default else "false"
    return str(default)


class TestEnvExampleMatchesSettingsDefaults:
    def test_every_settings_field_appears_in_env_example_or_is_explicitly_excluded(self):
        example = _parse_env_example()
        example_keys = {k.lower() for k in example}
        missing = [
            name
            for name in Settings.model_fields
            if name not in _EXCLUDED_FROM_ENV_EXAMPLE and name not in example_keys
        ]
        assert not missing, (
            f"these Settings fields have no line in .env.example: {missing} — "
            "add one, or add the field to _EXCLUDED_FROM_ENV_EXAMPLE with a reason"
        )

    def test_every_env_example_value_matches_the_settings_default(self):
        example = _parse_env_example()
        mismatches = []
        for env_key, example_value in example.items():
            field_name = env_key.lower()
            if field_name not in Settings.model_fields:
                continue  # not one of ours to check here
            expected = _field_default_as_env_string(field_name)
            if example_value != expected:
                mismatches.append((env_key, example_value, expected))
        assert not mismatches, (
            "these .env.example values have drifted from the code default "
            f"(key, example value, actual default): {mismatches}"
        )

    def test_min_order_paise_specifically_no_longer_implies_a_minimum_that_does_not_exist(self):
        """The exact drift the audit called out by name: the example used to
        suggest a ₹99 minimum order that the code has never actually
        enforced (min_order_paise defaults to 0 — no minimum)."""
        example = _parse_env_example()
        assert example["MIN_ORDER_PAISE"] == "0"

    def test_excluded_fields_are_still_real_fields(self):
        """Guards the exclusion list itself against going stale — if a field
        it names gets renamed or removed, this fails instead of the
        exclusion silently doing nothing."""
        for name in _EXCLUDED_FROM_ENV_EXAMPLE:
            assert name in Settings.model_fields
