"""Install the pinned sherpa wheels on an Alpine/musl target.

The upstream ARM64 wheels are manylinux wheels.  Alpine's pip correctly
rejects them, but the extension can run with gcompat when its bundled
glibc ONNX Runtime and ALSA copy are replaced by the Alpine packages.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import os
from pathlib import Path
import shutil
import sys
import zipfile


class InstallError(RuntimeError):
    pass


def _wheel(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise InstallError(f"expected one wheel matching {pattern!r}, found {len(matches)}")
    return matches[0]


def _extract(wheel: Path, destination: Path) -> None:
    with zipfile.ZipFile(wheel) as bundle:
        for member in bundle.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                raise InstallError(f"unsafe wheel member: {member.filename}")
            target = destination / path
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)


def install(resources: Path, destination: Path) -> Path:
    if not sys.platform.startswith("linux") or "musl" not in sys.version.lower():
        # Alpine's Python build does not always include the libc name in the
        # version string, so this is advisory rather than a hard platform gate.
        pass
    wheels = resources / "wheels"
    tag = f"cp{sys.version_info.major}{sys.version_info.minor}"
    python_wheel = _wheel(wheels / f"linux-aarch64-{tag}", f"sherpa_onnx-*.whl")
    core_wheel = _wheel(wheels / "linux-aarch64-core", "sherpa_onnx_core-*.whl")
    staging = destination.with_name(destination.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _extract(core_wheel, staging)
    _extract(python_wheel, staging)

    package = staging / "sherpa_onnx"
    extension = next(package.glob("lib/_sherpa_onnx*.so"), None)
    if extension is None:
        raise InstallError("sherpa Python extension is missing from the wheel")
    suffix = next(iter(importlib.machinery.EXTENSION_SUFFIXES), ".so")
    compatible = package / "lib" / ("_sherpa_onnx" + suffix)
    if extension != compatible:
        extension.rename(compatible)
    (package / "lib" / "__init__.py").touch()

    # Use Alpine's musl-compatible ONNX Runtime and ALSA instead of the
    # manylinux copies bundled in the wheel.
    bundled_runtime = package / "lib" / "libonnxruntime.so"
    system_runtime = Path("/usr/lib/libonnxruntime.so.1")
    if not system_runtime.exists():
        raise InstallError("/usr/lib/libonnxruntime.so.1 is missing; install py3-onnxruntime")
    bundled_runtime.unlink(missing_ok=True)
    bundled_runtime.symlink_to(system_runtime)
    bundled_alsa = next((staging / "sherpa_onnx.libs").glob("libasound-*.so.*"), None)
    system_alsa = Path("/lib/libasound.so.2.0.0")
    if bundled_alsa is not None and system_alsa.exists():
        bundled_alsa.unlink()
        bundled_alsa.symlink_to(system_alsa)

    if destination.exists():
        shutil.rmtree(destination)
    staging.rename(destination)
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resources", type=Path, required=True)
    parser.add_argument("--destination", type=Path, default=Path(".voice-runtime/sherpa"))
    args = parser.parse_args()
    try:
        destination = install(args.resources, args.destination)
        print(destination.resolve())
        print("PYTHONPATH=" + str(destination.resolve()))
        print("LD_LIBRARY_PATH=" + os.pathsep.join((str(destination / "sherpa_onnx" / "lib"), str(destination / "sherpa_onnx.libs"), "/lib", "/usr/lib")))
    except (OSError, zipfile.BadZipFile, InstallError) as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
