"""Read the environment policy for test results."""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex

from scripts.lib import conf


@dataclass(frozen=True)
class TestEnvironmentPolicy:
    """Select result inputs and replace terminal values for each test."""

    names: tuple[str, ...]
    prefixes: tuple[str, ...]
    fixed: tuple[tuple[str, str], ...]

    @property
    def forwarded_names(self) -> tuple[str, ...]:
        return (*self.names, "RUST_LOG", "RUST_BACKTRACE", "RUST_LIB_BACKTRACE")

    def selects(self, name: str) -> bool:
        return name in self.names or name.startswith(self.prefixes)

    def clear_remote_environment(self) -> str:
        alternatives = [re.escape(name) for name in self.forwarded_names]
        alternatives.extend(re.escape(prefix) + "[A-Za-z0-9_]*" for prefix in self.prefixes)
        program = '/^(' + '|'.join(alternatives) + ')=/ {print "unset " $1}'
        return 'eval "$(env | awk -F= ' + shlex.quote(program) + ')"'


def read_policy() -> TestEnvironmentPolicy:
    configuration = conf.read("direct")

    def policy_words(key: str) -> tuple[str, ...]:
        settings = configuration.settings
        if isinstance(settings, dict) and key in settings and not settings[key].value:
            return ()
        return configuration.words(key)

    return TestEnvironmentPolicy(
        policy_words("VERIFY_RESULT_ENV_NAMES"),
        policy_words("VERIFY_RESULT_ENV_PREFIXES"),
        tuple((name, value) for entry in policy_words("VERIFY_TEST_FIXED_ENV")
              for name, _, value in (entry.partition("="),)),
    )
