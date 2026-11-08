"""Keep Cargo output reads inside the test fixture."""

from __future__ import annotations

from collections.abc import Generator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scripts.lib import cargo_output
from scripts.lib.cargo_output import CargoOutputRoots, CargoOutputUnavailable


def restore_environment(name: str, value: str | None) -> None:
    if value is None:
        _ = os.environ.pop(name, None)
    else:
        os.environ[name] = value


@contextmanager
def fake_cargo_output_roots(
    *mappings: CargoOutputRoots, children: bool = False,
) -> Generator[None]:
    """Use supplied mappings and report unavailable for every other checkout."""
    reader = cargo_output.read_cargo_output_roots

    def read(checkout: Path, *, cargo: Sequence[str] = ("cargo",), options: Sequence[str] = (),
             environment: Mapping[str, str] | None = None) -> CargoOutputRoots | CargoOutputUnavailable:
        del cargo, options, environment
        resolved = checkout.resolve()
        for roots in mappings:
            if resolved == roots.workspace_root:
                return roots
        return CargoOutputUnavailable("the fixture has no Cargo output mapping")

    with ExitStack() as stack:
        if children:
            startup = Path(stack.enter_context(TemporaryDirectory(prefix="cargo-output-fixture-")))
            arguments = ", ".join(
                f"CargoOutputRoots(Path({str(roots.workspace_root)!r}), "
                + f"Path({str(roots.target_directory)!r}), Path({str(roots.build_directory)!r}))"
                for roots in mappings
            )
            # Python children enter the same boundary mock before loading their scripts.
            _ = (startup / "sitecustomize.py").write_text(
                "from pathlib import Path\n"
                + "from scripts.lib.cargo_output_test_support import fake_cargo_output_roots, CargoOutputRoots\n"
                + f"_cargo_output_fixture = fake_cargo_output_roots({arguments})\n"
                + "_cargo_output_fixture.__enter__()\n"
            )
            python_path = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[2]),
                                                       str(startup), os.environ.get("PYTHONPATH", ""))))
            previous = os.environ.get("PYTHONPATH")
            os.environ["PYTHONPATH"] = python_path
            _ = stack.callback(restore_environment, "PYTHONPATH", previous)
        # Patch each imported reader binding.
        for module in tuple(sys.modules.values()):
            binding = vars(module).get("read_cargo_output_roots")
            if binding is reader:
                _ = stack.enter_context(patch.object(module, "read_cargo_output_roots", side_effect=read))
        yield
