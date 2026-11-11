"""Name herdr failures and native identity marks without loading the herdr controller."""

NATIVE_ID_MARKS = ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID")


class HerdrFailure(Exception):
    """An external command failure or malformed herdr response."""

    def __init__(self, message: str, code: str = "") -> None:
        super().__init__(message)
        self.code: str = code


class HerdrMissing(HerdrFailure):
    """No herdr binary is available."""
