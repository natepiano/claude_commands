"""Generate the shared removal command for abandoned compiler sessions."""

import shlex


def removal_script(target: str, minutes: int) -> str:
    """Return a silent removal group for the caller's quoted build folder."""
    remove_sessions = 'for session do rm -rf -- "$session" "${session%-working}.lock"; done'
    return (
        f"{{ find {target} -maxdepth 5 -type d "
        + r"\( -name deps -o -name build -o -name .fingerprint -o -name examples \) -prune -o "
        + f"-type d -name 's-*-working' -mmin +{minutes} -prune "
        + f"-exec sh -c {shlex.quote(remove_sessions)} sh {{}} + >/dev/null 2>&1 || :; }}"
    )
