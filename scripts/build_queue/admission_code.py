"""Hash the admission sources without importing admission code."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sys


ADMISSION_CODE_PATHS = (
    "direct/verify.sh", "lint/invoke.sh", "hooks/pre-tool-use-brp-launch-gate.sh",
    "production/merge_gate.py", "production/targeted_test.py", "lint/memory_gate.sh", "lint/memory_admit.py",
    "build_queue/admission_code.py", "build_queue/admission_client.py", "build_queue/admission_protocol.py",
    "build_queue/system_clock.py", "build_queue/admission_records.py", "build_queue/step_estimator.py",
    "build_queue/admission_service.py", "build_queue/queue_place.py",
    "build_queue/scope_activity.py", "build_queue/queue_snapshot.py", "build_queue/progress_profile.py", "build_queue/snapshot_codec.py",
    "lib/suite_runners.py", "lib/build_steps.py", "lib/boot_identity.py", "lib/inotify.py", "lib/conf.py", "lib/atomic_file.py", "lib/test_phase.py",
    "lint/build_shape.py", "buildlog/store.py", "buildlog/record.py", "buildlog/repository.py", "buildlog/parse.py",
)


def admission_code_version(scripts: Path | None = None) -> str:
    root = scripts if scripts is not None else Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for relative in ADMISSION_CODE_PATHS:
        try:
            content = (root / relative).read_bytes()
        except OSError:
            return ""
        digest.update(relative.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()


if __name__ == "__main__":
    version = admission_code_version()
    if not version:
        sys.exit("admission code is unavailable")
    print(version)
