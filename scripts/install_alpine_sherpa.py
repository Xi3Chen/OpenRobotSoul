"""Build and install the pinned sherpa-onnx Python extension on Alpine/musl.

The upstream ARM64 wheels are manylinux wheels built against glibc.  Alpine's
musl Python cannot load them, and swapping their bundled glibc ONNX Runtime and
ALSA libraries for Alpine packages does not make them loadable either (that
approach was verified to fail on the MiMax2 target and has been removed).

This script instead performs a native build of the Python extension from the
pinned sherpa-onnx source (commit af2d8d81dbfa61c4cea4c568da78f3982edca005,
upstream tag v2.0.1) against the target's own musl ONNX Runtime, installs the
result into runtime/sherpa-musl, and verifies that ``import sherpa_onnx`` works
with only musl system libraries.

Prerequisites on the Alpine aarch64 target:

* CPython 3.11+ (the same interpreter that will run OpenRobotSoul; the
  extension ABI is tied to it) and its development headers (``python3-dev``).
* cmake >= 3.15, a C++17 compiler (``build-base``) and make or ninja.
* The target's musl ONNX Runtime and its C++ headers, e.g. ``apk add
  py3-onnxruntime onnxruntime-dev`` (or point --onnxruntime-lib-dir /
  --onnxruntime-include-dir, or set SHERPA_ONNXRUNTIME_LIB_DIR and
  SHERPA_ONNXRUNTIME_INCLUDE_DIR at the matching locations).
* At least 2 GiB free where the build tree lives.  The build needs network
  access for CMake's third-party downloads (or a pre-populated cache).

Usage on the target::

    python scripts/install_alpine_sherpa.py --check
    python scripts/install_alpine_sherpa.py --dry-run
    python scripts/install_alpine_sherpa.py

The build must run on the target because the extension links the target's musl
libc and musl ONNX Runtime.  Windows/macOS hosts can only use --dry-run or
--check to inspect the plan.

Re-running the script is idempotent: when runtime/sherpa-musl already matches
the pinned source and interpreter, the build is skipped and the existing
installation is re-verified.  Use --rebuild to force a clean rebuild.
"""

from __future__ import annotations

import argparse
import importlib.machinery
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parent.parent
PINNED_VERSION = "v2.0.1"
PINNED_COMMIT = "af2d8d81dbfa61c4cea4c568da78f3982edca005"
DEFAULT_SOURCE = REPO_ROOT / "third_party" / "sherpa-onnx"
DEFAULT_BUILD_DIR = REPO_ROOT / "build" / "sherpa-musl"
DEFAULT_DESTINATION = REPO_ROOT / "runtime" / "sherpa-musl"
STAMP_NAME = ".sherpa-musl-build.json"
STAMP_SCHEMA_VERSION = 1

MIN_PYTHON = (3, 11)
MIN_CMAKE = (3, 15)
DEFAULT_MIN_FREE_GIB = 2.0

# Options that are pinned by this script so a rebuild is reproducible and does
# not silently change with upstream CMake defaults.  The Python binding needs
# TTS and speaker diarization because sherpa_onnx/__init__.py imports those
# classes unconditionally.  The extension is loaded from a directory
# (PYTHONPATH), so binaries, tests, the C API, PortAudio and WebSocket support
# are not needed.
CMAKE_OPTIONS: tuple[str, ...] = (
    "-DCMAKE_BUILD_TYPE=Release",
    "-DBUILD_SHARED_LIBS=ON",
    "-DSHERPA_ONNX_ENABLE_PYTHON=ON",
    "-DSHERPA_ONNX_ENABLE_BINARY=OFF",
    "-DSHERPA_ONNX_ENABLE_TESTS=OFF",
    "-DSHERPA_ONNX_ENABLE_CHECK=OFF",
    "-DSHERPA_ONNX_ENABLE_C_API=OFF",
    "-DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF",
    "-DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF",
    "-DSHERPA_ONNX_ENABLE_TTS=ON",
    "-DSHERPA_ONNX_ENABLE_SPEAKER_DIARIZATION=ON",
    "-DSHERPA_ONNX_USE_PRE_INSTALLED_ONNXRUNTIME_IF_AVAILABLE=ON",
)

VERIFY_SOURCE = (
    "import sherpa_onnx\n"
    "print('sherpa_onnx', sherpa_onnx.version())\n"
    "print('onnxruntime', sherpa_onnx.onnxruntime_version())\n"
)


class InstallError(RuntimeError):
    """A preflight, build or install failure with an actionable message."""


def _run(command: list[str], *, env: dict[str, str] | None = None, hint: str = "") -> None:
    printable = shlex.join(str(part) for part in command)
    print(f"+ {printable}", flush=True)
    try:
        result = subprocess.run(command, env=env)
    except OSError as exc:
        message = f"could not start {command[0]}: {exc}"
        raise InstallError(message + (f"\n{hint}" if hint else "")) from exc
    if result.returncode != 0:
        message = f"command failed with exit code {result.returncode}: {printable}"
        raise InstallError(message + (f"\n{hint}" if hint else ""))


def _query_interpreter(python: Path) -> dict[str, Any]:
    script = (
        "import os, sys, sysconfig\n"
        "print(sys.version_info[0])\n"
        "print(sys.version_info[1])\n"
        "print(sysconfig.get_config_var('EXT_SUFFIX') or '')\n"
        "include = sysconfig.get_paths().get('include', '')\n"
        "print(include)\n"
        "print(int(bool(include) and os.path.isfile(os.path.join(include, 'Python.h'))))\n"
    )
    try:
        result = subprocess.run(
            [str(python), "-c", script], capture_output=True, text=True, timeout=30
        )
    except OSError as exc:
        raise InstallError(f"could not run the target interpreter {python}: {exc}") from exc
    except subprocess.SubprocessError as exc:
        raise InstallError(f"could not query the target interpreter {python}: {exc}") from exc
    if result.returncode != 0:
        raise InstallError(
            f"the target interpreter {python} could not be queried: {result.stderr.strip()}"
        )
    lines = result.stdout.splitlines()
    if len(lines) < 2 or not lines[0].isdigit() or not lines[1].isdigit():
        raise InstallError(f"unexpected output from {python}: {result.stdout!r}")
    major, minor = int(lines[0]), int(lines[1])
    suffix = lines[2] if len(lines) > 2 else ""
    include = Path(lines[3]) if len(lines) > 3 and lines[3] else None
    headers_ok = len(lines) > 4 and lines[4].strip() == "1"
    return {
        "major": major,
        "minor": minor,
        "tag": f"cp{major}{minor}",
        "extension_suffix": suffix or (importlib.machinery.EXTENSION_SUFFIXES[0] if importlib.machinery.EXTENSION_SUFFIXES else ".so"),
        "include": include,
        "headers_ok": headers_ok,
    }


def _detect_musl() -> bool:
    for directory in (Path("/lib"), Path("/usr/lib")):
        try:
            if directory.is_dir() and any(directory.glob("ld-musl-*.so*")):
                return True
        except OSError:
            continue
    ldd = shutil.which("ldd")
    if ldd:
        try:
            result = subprocess.run(
                [ldd, "--version"], capture_output=True, text=True, timeout=10
            )
        except (OSError, subprocess.SubprocessError):
            result = None
        if result is not None:
            output = (result.stdout + result.stderr).lower()
            if "musl" in output:
                return True
    return False


def _cmake_version(cmake: str) -> tuple[int, int] | None:
    try:
        result = subprocess.run(
            [cmake, "--version"], capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    first = result.stdout.splitlines()[0] if result.stdout else ""
    parts = first.split()
    if len(parts) < 3:
        return None
    try:
        major, minor = parts[2].split(".")[:2]
        return int(major), int(minor)
    except (ValueError, IndexError):
        return None


def _nearest_existing(path: Path) -> Path:
    current = path
    while True:
        try:
            if current.exists():
                return current
        except OSError:
            return current
        if current == current.parent:
            return current
        current = current.parent


def _free_gib(path: Path) -> float:
    try:
        return shutil.disk_usage(_nearest_existing(path)).free / (1024 ** 3)
    except OSError:
        return 0.0


def _source_commit(source: Path) -> str | None:
    git = shutil.which("git")
    if not git:
        return None
    try:
        result = subprocess.run(
            [git, "-C", str(source), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    commit = result.stdout.strip()
    return commit or None


def _find_onnxruntime(
    lib_dir: Path | None, include_dir: Path | None
) -> tuple[Path | None, Path | None]:
    """Locate the target's (musl) ONNX Runtime library and C++ header."""
    lib_candidates: list[Path] = []
    if lib_dir is not None:
        lib_candidates.append(lib_dir)
    env_lib = os.environ.get("SHERPA_ONNXRUNTIME_LIB_DIR")
    if env_lib:
        lib_candidates.append(Path(env_lib))
    lib_candidates += [Path("/usr/lib"), Path("/lib"), Path("/usr/local/lib")]

    # CMake links against the unversioned development symlink, so only that
    # name satisfies the build requirement.  The versioned runtime library is
    # provided separately (e.g. Alpine's py3-onnxruntime) and is exercised by
    # the import verification step.
    library: Path | None = None
    for directory in lib_candidates:
        candidate = directory / "libonnxruntime.so"
        try:
            if candidate.exists():
                library = candidate
                break
        except OSError:
            continue

    header_candidates: list[Path] = []
    if include_dir is not None:
        header_candidates.append(include_dir)
    env_include = os.environ.get("SHERPA_ONNXRUNTIME_INCLUDE_DIR")
    if env_include:
        header_candidates.append(Path(env_include))
    header_candidates += [
        Path("/usr/include"),
        Path("/usr/local/include"),
        Path("/usr/include/onnxruntime"),
        Path("/usr/local/include/onnxruntime"),
    ]

    header: Path | None = None
    for directory in header_candidates:
        candidate = directory / "onnxruntime_cxx_api.h"
        try:
            if candidate.exists():
                header = candidate
                break
        except OSError:
            continue
    return library, header


def _target_facts(
    *, source: Path, python: Path, onnxruntime_lib_dir: Path | None,
    onnxruntime_include_dir: Path | None, expected_commit: str,
) -> dict[str, Any]:
    interpreter = _query_interpreter(python)
    library, header = _find_onnxruntime(onnxruntime_lib_dir, onnxruntime_include_dir)
    return {
        "python": python,
        "python_major": interpreter["major"],
        "python_minor": interpreter["minor"],
        "python_tag": interpreter["tag"],
        "extension_suffix": interpreter["extension_suffix"],
        "python_include": interpreter["include"],
        "python_headers_ok": interpreter["headers_ok"],
        "source": source,
        "source_commit": _source_commit(source),
        "expected_commit": expected_commit,
        "onnxruntime_lib": library,
        "onnxruntime_include": header,
    }


def _platform_problems() -> list[str]:
    problems: list[str] = []
    if platform.system() != "Linux":
        problems.append(
            f"this native build must run on the target (Linux aarch64/musl, e.g. Alpine); "
            f"the current host is {platform.system()} {platform.machine()}. "
            "The build links the target's musl libc and musl ONNX Runtime and cannot be "
            "cross-built here. Use --dry-run on this host to print the plan instead."
        )
        return problems
    if platform.machine().lower() not in {"aarch64", "arm64"}:
        problems.append(
            f"target architecture must be aarch64, found {platform.machine()!r}; "
            "the pinned sherpa-onnx sources build the ARM64 extension only."
        )
    if not _detect_musl():
        problems.append(
            "musl libc was not detected. This script only supports Alpine/musl targets; "
            "on a glibc host (manylinux) install the published wheels instead, and do not "
            "use this script's output there."
        )
    return problems


def _environment_problems(
    facts: dict[str, Any], *, build_dir: Path, destination: Path, min_free_gib: float,
) -> tuple[list[str], list[str]]:
    problems: list[str] = []
    warnings: list[str] = []

    if (facts["python_major"], facts["python_minor"]) < MIN_PYTHON:
        problems.append(
            f"target interpreter {facts['python']} is CPython "
            f"{facts['python_major']}.{facts['python_minor']}; OpenRobotSoul requires >= "
            f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}."
        )
    if not facts["python_headers_ok"]:
        problems.append(
            "the Python development headers (Python.h) for the target interpreter were "
            f"not found under {facts['python_include']}. The Python binding is compiled "
            "from source and needs them: install the development package for that "
            "interpreter (Alpine: `apk add python3-dev`)."
        )

    cmake = shutil.which("cmake")
    if not cmake:
        problems.append(
            "cmake was not found on PATH; install it (Alpine: `apk add cmake`) "
            f"and ensure version >= {MIN_CMAKE[0]}.{MIN_CMAKE[1]}."
        )
    else:
        version = _cmake_version(cmake)
        if version is None:
            problems.append(f"could not determine the cmake version from {cmake}.")
        elif version < MIN_CMAKE:
            problems.append(
                f"cmake {version[0]}.{version[1]} is too old; sherpa-onnx needs >= "
                f"{MIN_CMAKE[0]}.{MIN_CMAKE[1]} (Alpine: `apk add cmake`)."
            )

    if not any(shutil.which(compiler) for compiler in ("c++", "g++", "clang++")):
        problems.append(
            "no C++ compiler found on PATH; install a C++17 toolchain "
            "(Alpine: `apk add build-base`)."
        )
    if not any(shutil.which(generator) for generator in ("make", "ninja")):
        problems.append(
            "neither make nor ninja was found; install one "
            "(Alpine: `apk add build-base` or `apk add ninja`)."
        )

    if facts["onnxruntime_lib"] is None:
        problems.append(
            "the target's musl ONNX Runtime development library was not found (looked "
            "for libonnxruntime.so in --onnxruntime-lib-dir, "
            "SHERPA_ONNXRUNTIME_LIB_DIR, /usr/lib, /lib, /usr/local/lib). Install it on "
            "the target (Alpine: `apk add onnxruntime-dev`) or pass "
            "--onnxruntime-lib-dir. The versioned runtime library libonnxruntime.so.1 "
            "comes from `apk add py3-onnxruntime` and is required at import time."
        )
    if facts["onnxruntime_include"] is None:
        problems.append(
            "the ONNX Runtime C++ headers were not found (looked for "
            "onnxruntime_cxx_api.h in --onnxruntime-include-dir, "
            "SHERPA_ONNXRUNTIME_INCLUDE_DIR, /usr/include, /usr/local/include). "
            "Install the matching development headers (Alpine: `apk add onnxruntime-dev`) "
            "or pass --onnxruntime-include-dir."
        )

    if facts["source_commit"] is None:
        warnings.append(
            f"could not verify the sherpa-onnx source commit at {facts['source']} "
            "(git or .git missing). Continuing with the checked-out tree."
        )
    elif facts["source_commit"] != facts["expected_commit"]:
        problems.append(
            f"source checkout is at commit {facts['source_commit']}, but this script pins "
            f"{facts['expected_commit']} ({PINNED_VERSION}). Restore the pinned submodule "
            f"with `git -C {REPO_ROOT} submodule update --init third_party/sherpa-onnx` "
            "(or check out the pinned commit in --source)."
        )

    free = _free_gib(build_dir)
    if free < min_free_gib:
        problems.append(
            f"only {free:.1f} GiB free under {_nearest_existing(build_dir)}; the native "
            f"build tree needs about {min_free_gib:.1f} GiB. Pass --build-dir on a "
            "partition with more space (the MiMax2 root partition is nearly full; build "
            "under /home/user or external storage instead of /)."
        )

    destination_free = _free_gib(destination.parent)
    if destination_free < 0.05:
        warnings.append(
            f"only {destination_free * 1024:.0f} MiB free under the destination; the "
            "installed runtime is small but the filesystem is nearly full."
        )
    return problems, warnings


def _configure_command(
    source: Path, build_dir: Path, python: Path
) -> list[str]:
    return [
        "cmake",
        "-S",
        str(source),
        "-B",
        str(build_dir),
        *CMAKE_OPTIONS,
        f"-DPYTHON_EXECUTABLE={python}",
    ]


def _build_command(build_dir: Path, jobs: int) -> list[str]:
    return [
        "cmake",
        "--build",
        str(build_dir),
        "--target",
        "_sherpa_onnx",
        "--parallel",
        str(jobs),
    ]


def _build_environment(facts: dict[str, Any]) -> dict[str, str]:
    env = os.environ.copy()
    library = facts["onnxruntime_lib"]
    header = facts["onnxruntime_include"]
    if library is not None:
        env["SHERPA_ONNXRUNTIME_LIB_DIR"] = str(library.parent)
    if header is not None:
        env["SHERPA_ONNXRUNTIME_INCLUDE_DIR"] = str(header.parent)
    return env


def _built_extension(build_dir: Path) -> Path:
    lib_dir = build_dir / "lib"
    matches = sorted(lib_dir.glob("_sherpa_onnx*.so")) if lib_dir.is_dir() else []
    if not matches:
        matches = sorted(build_dir.rglob("_sherpa_onnx*.so"))
    if not matches:
        raise InstallError(
            f"the build finished but no _sherpa_onnx*.so was found under {build_dir}. "
            "Inspect the build output above; a common cause is a failed pybind11/Python "
            "bindings step."
        )
    if len(matches) > 1:
        listing = "\n".join(f"  {path}" for path in matches)
        raise InstallError(
            "multiple built extensions were found; the build directory mixes "
            f"different interpreters or stale artifacts:\n{listing}\n"
            "Re-run with --rebuild to configure a clean build tree."
        )
    return matches[0]


def _shared_libraries(build_dir: Path) -> list[Path]:
    lib_dir = build_dir / "lib"
    if not lib_dir.is_dir():
        raise InstallError(
            f"the build finished but {lib_dir} does not exist; the expected shared "
            "libraries were not produced."
        )
    bundled_runtime = sorted(lib_dir.glob("libonnxruntime*"))
    if bundled_runtime:
        listing = "\n".join(f"  {path}" for path in bundled_runtime)
        raise InstallError(
            "the build tree contains a bundled ONNX Runtime library:\n"
            f"{listing}\n"
            "That library is the glibc build CMake downloads by default and cannot be "
            "loaded by musl Python. The configure step did not use the target's musl "
            "ONNX Runtime. Install it (Alpine: `apk add py3-onnxruntime onnxruntime-dev`), "
            "pass --onnxruntime-lib-dir/--onnxruntime-include-dir, then rerun with "
            "--rebuild."
        )
    return sorted(
        path
        for path in lib_dir.iterdir()
        if path.is_file()
        and not path.name.startswith("_sherpa_onnx")
        and (path.name.endswith(".so") or ".so." in path.name)
    )


def _install_package(
    *,
    source: Path,
    build_dir: Path,
    destination: Path,
    stamp: dict[str, Any],
) -> None:
    package_source = source / "sherpa-onnx" / "python" / "sherpa_onnx"
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.with_name(destination.name + ".staging")
    previous = destination.with_name(destination.name + ".previous")

    if staging.exists():
        shutil.rmtree(staging)
    if previous.exists():
        shutil.rmtree(previous)

    print(f"assembling {destination} (staging: {staging})")
    shutil.copytree(
        package_source,
        staging / "sherpa_onnx",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    lib_dir = staging / "sherpa_onnx" / "lib"
    lib_dir.mkdir()
    (lib_dir / "__init__.py").touch()

    extension = _built_extension(build_dir)
    shutil.copy2(extension, lib_dir / extension.name)
    print(f"  extension: {extension.name}")
    for library in _shared_libraries(build_dir):
        shutil.copy2(library, lib_dir / library.name)
        print(f"  library:   {library.name}")

    (staging / STAMP_NAME).write_text(
        json.dumps(stamp, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _runtime_environment(destination: Path) -> dict[str, str]:
    env = os.environ.copy()
    lib_dir = destination / "sherpa_onnx" / "lib"
    env["LD_LIBRARY_PATH"] = os.pathsep.join(
        [str(lib_dir), "/lib", "/usr/lib"] + ([env["LD_LIBRARY_PATH"]] if env.get("LD_LIBRARY_PATH") else [])
    )
    env["PYTHONPATH"] = os.pathsep.join(
        [str(destination)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    return env


def _verify(destination: Path, python: Path) -> str:
    env = _runtime_environment(destination)
    try:
        result = subprocess.run(
            [str(python), "-c", VERIFY_SOURCE],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(
            f"could not run the import verification with {python}: {exc}"
        ) from exc
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise InstallError(
            "the installed sherpa_onnx package cannot be imported on this target.\n"
            f"interpreter: {python}\n"
            f"stderr:\n{stderr[-4000:]}\n"
            "Check that the target's musl ONNX Runtime runtime library is installed "
            "and loadable (Alpine: `apk add py3-onnxruntime` provides "
            "/usr/lib/libonnxruntime.so.1), that it was not replaced by a glibc copy, "
            "and that the interpreter is the same ABI the extension was built for. "
            "Re-run with --rebuild after fixing the ONNX Runtime installation."
        )
    output = result.stdout.strip()
    if not output:
        raise InstallError("the import verification produced no output.")
    return output


def _expected_stamp(facts: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": STAMP_SCHEMA_VERSION,
        "sherpa_onnx_version": PINNED_VERSION,
        "sherpa_onnx_commit": facts["source_commit"] or facts["expected_commit"],
        "python_tag": facts["python_tag"],
        "extension_suffix": facts["extension_suffix"],
        "onnxruntime_library": str(facts["onnxruntime_lib"]) if facts["onnxruntime_lib"] else None,
        "cmake_options": list(CMAKE_OPTIONS),
    }


def _read_stamp(destination: Path) -> dict[str, Any] | None:
    stamp_path = destination / STAMP_NAME
    try:
        value = json.loads(stamp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _extension_installed(destination: Path) -> bool:
    lib_dir = destination / "sherpa_onnx" / "lib"
    return lib_dir.is_dir() and any(lib_dir.glob("_sherpa_onnx*.so"))


# Only these stamp keys decide whether the installed runtime is current.  The
# ONNX Runtime development library is a build input and is deliberately not
# part of the comparison: a target may remove build packages after installing,
# and the verification step below still proves the installed runtime loads.
STAMP_MATCH_KEYS = (
    "schema_version",
    "sherpa_onnx_version",
    "sherpa_onnx_commit",
    "python_tag",
    "extension_suffix",
    "cmake_options",
)


def _is_current(destination: Path, facts: dict[str, Any]) -> bool:
    if not _extension_installed(destination):
        return False
    stamp = _read_stamp(destination)
    if stamp is None:
        return False
    expected = _expected_stamp(facts)
    return all(stamp.get(key) == expected[key] for key in STAMP_MATCH_KEYS)


def _print_runtime_paths(destination: Path) -> None:
    lib_dir = destination / "sherpa_onnx" / "lib"
    print(destination)
    print(f"PYTHONPATH={destination}")
    print(f"LD_LIBRARY_PATH={os.pathsep.join([str(lib_dir), '/lib', '/usr/lib'])}")


def _print_plan(
    *, source: Path, build_dir: Path, destination: Path, python: Path, jobs: int,
    min_free_gib: float,
) -> None:
    print("dry-run: no files will be written.")
    print(f"  repository:        {REPO_ROOT}")
    print(f"  source:            {source}")
    print(f"  build dir:         {build_dir} (free space required: {min_free_gib:.1f} GiB)")
    print(f"  destination:       {destination}")
    print(f"  interpreter:       {python}")
    print(f"  pinned sherpa:     {PINNED_VERSION} ({PINNED_COMMIT})")
    print("  onnxruntime:       resolved from the target at preflight (musl lib + headers)")
    print("would run:")
    print("  " + shlex.join(_configure_command(source, build_dir, python)))
    print("  " + shlex.join(_build_command(build_dir, jobs)))
    print("would install:")
    print(f"  {destination / 'sherpa_onnx'}/*.py from {source / 'sherpa-onnx' / 'python' / 'sherpa_onnx'}")
    print(f"  {destination / 'sherpa_onnx' / 'lib'}/*.so* from {build_dir / 'lib'}")
    print(f"  {destination / STAMP_NAME} (build stamp)")
    print("would verify:")
    print(
        "  "
        + f"PYTHONPATH={destination} LD_LIBRARY_PATH="
        + os.pathsep.join([str(destination / 'sherpa_onnx' / 'lib'), '/lib', '/usr/lib'])
        + " "
        + f"{python} -c "
        + shlex.quote(
            "import sherpa_onnx; print('sherpa_onnx', sherpa_onnx.version()); "
            "print('onnxruntime', sherpa_onnx.onnxruntime_version())"
        )
    )
    print("This build requires network access for CMake's third-party downloads.")


def _report(problems: list[str], warnings: list[str], label: str) -> None:
    for warning in warnings:
        print(f"  warn {warning}")
    for problem in problems:
        print(f"  FAIL {problem}")
    if problems:
        raise InstallError(f"{label} failed: {len(problems)} problem(s) listed above.")
    print(f"{label}: ok" + (f" ({len(warnings)} warning(s))" if warnings else ""))


def _run_checks(
    *, source: Path, build_dir: Path, destination: Path, python: Path,
    min_free_gib: float, onnxruntime_lib_dir: Path | None,
    onnxruntime_include_dir: Path | None, expected_commit: str,
) -> dict[str, Any]:
    print("== preflight ==")
    problems = _platform_problems()
    if problems:
        _report(problems, [], "preflight")

    facts = _target_facts(
        source=source,
        python=python,
        onnxruntime_lib_dir=onnxruntime_lib_dir,
        onnxruntime_include_dir=onnxruntime_include_dir,
        expected_commit=expected_commit,
    )
    environment_problems, warnings = _environment_problems(
        facts, build_dir=build_dir, destination=destination, min_free_gib=min_free_gib
    )
    print(
        f"  ok   platform: {platform.system()} {platform.machine()} (musl)"
    )
    print(
        f"  ok   python: {facts['python']} "
        f"({facts['python_tag']}, {facts['extension_suffix']})"
    )
    if facts["source_commit"]:
        print(f"  ok   source commit: {facts['source_commit']}")
    library = facts["onnxruntime_lib"].parent if facts["onnxruntime_lib"] else None
    header = facts["onnxruntime_include"].parent if facts["onnxruntime_include"] else None
    if library is not None and header is not None:
        print(f"  ok   ONNX Runtime: {library} (lib), {header} (headers)")
    print(f"  ok   build space: {_free_gib(build_dir):.1f} GiB free")
    _report(environment_problems, warnings, "preflight")
    return facts


def _build_and_install(
    *,
    facts: dict[str, Any],
    build_dir: Path,
    jobs: int,
    destination: Path,
    rebuild: bool,
    skip_verify: bool,
) -> None:
    source = facts["source"]
    python = facts["python"]

    if rebuild and build_dir.exists():
        print(f"removing stale build tree: {build_dir}")
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True, exist_ok=True)

    environment = _build_environment(facts)

    print("== configure ==")
    _run(
        _configure_command(source, build_dir, python),
        env=environment,
        hint=(
            "If CMake cannot use the system ONNX Runtime, pass "
            "--onnxruntime-lib-dir/--onnxruntime-include-dir. A configure failure about "
            "onnxruntime usually means its musl build or headers are missing."
        ),
    )

    print("== build ==")
    _run(
        _build_command(build_dir, jobs),
        env=environment,
        hint=(
            "A compile error mentioning onnxruntime headers means the installed musl "
            "ONNX Runtime version is not compatible with sherpa-onnx " + PINNED_VERSION + "; "
            "install matching headers or use --onnxruntime-include-dir."
        ),
    )

    print("== install ==")
    stamp = _expected_stamp(facts)
    stamp["built_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    _install_package(
        source=source, build_dir=build_dir, destination=destination, stamp=stamp
    )

    staging = destination.with_name(destination.name + ".staging")
    if not skip_verify:
        print("== verify ==")
        try:
            print(_verify(staging, python))
        except InstallError:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    previous = destination.with_name(destination.name + ".previous")
    if destination.exists():
        destination.rename(previous)
    try:
        staging.rename(destination)
    except OSError as exc:
        if previous.exists() and not destination.exists():
            previous.rename(destination)
        raise InstallError(
            f"could not move the staged installation into {destination}: {exc}"
        ) from exc
    if previous.exists():
        shutil.rmtree(previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=DEFAULT_SOURCE,
        help="sherpa-onnx source checkout (default: third_party/sherpa-onnx)",
    )
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=DEFAULT_BUILD_DIR,
        help="CMake build tree (default: build/sherpa-musl)",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=DEFAULT_DESTINATION,
        help="installed runtime directory (default: runtime/sherpa-musl)",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=Path(sys.executable),
        help="target interpreter the extension is built for (default: current interpreter)",
    )
    parser.add_argument(
        "--jobs", type=int, default=os.cpu_count() or 1, help="parallel build jobs"
    )
    parser.add_argument(
        "--min-free-gib",
        type=float,
        default=DEFAULT_MIN_FREE_GIB,
        help="minimum free space required for the build tree (default: 2.0)",
    )
    parser.add_argument(
        "--onnxruntime-lib-dir",
        type=Path,
        help="directory containing the target's musl libonnxruntime.so",
    )
    parser.add_argument(
        "--onnxruntime-include-dir",
        type=Path,
        help="directory containing onnxruntime_cxx_api.h",
    )
    parser.add_argument(
        "--expected-commit",
        default=PINNED_COMMIT,
        help="expected sherpa-onnx source commit (default: pinned v2.0.1 commit)",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    parser.add_argument(
        "--check", action="store_true", help="run the preflight checks and exit"
    )
    parser.add_argument(
        "--rebuild", action="store_true", help="remove the build tree and rebuild"
    )
    parser.add_argument(
        "--skip-verify", action="store_true", help="skip the import smoke test"
    )
    parser.add_argument(
        "--resources",
        type=Path,
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    if args.resources is not None:
        parser.error(
            "--resources was removed: the manylinux wheel replacement flow cannot work "
            "on Alpine/musl. Remove --resources; this script now builds sherpa-onnx from "
            "the pinned source (see docs/deployment-guide.zh-CN.md section 8.1)."
        )
    if args.jobs < 1:
        parser.error("--jobs must be >= 1")
    if args.min_free_gib < 0:
        parser.error("--min-free-gib must be >= 0")

    source = args.source.resolve()
    build_dir = args.build_dir.resolve()
    destination = args.destination.resolve()
    python = args.python
    if not python.is_absolute():
        found = shutil.which(str(python))
        python = Path(found) if found else python
    python = python.resolve() if python.exists() else python

    if args.dry_run:
        _print_plan(
            source=source,
            build_dir=build_dir,
            destination=destination,
            python=python,
            jobs=args.jobs,
            min_free_gib=args.min_free_gib,
        )
        return 0

    try:
        if args.check:
            _run_checks(
                source=source,
                build_dir=build_dir,
                destination=destination,
                python=python,
                min_free_gib=args.min_free_gib,
                onnxruntime_lib_dir=args.onnxruntime_lib_dir,
                onnxruntime_include_dir=args.onnxruntime_include_dir,
                expected_commit=args.expected_commit,
            )
            return 0

        platform_problems = _platform_problems()
        if platform_problems:
            _report(platform_problems, [], "preflight")

        facts = _target_facts(
            source=source,
            python=python,
            onnxruntime_lib_dir=args.onnxruntime_lib_dir,
            onnxruntime_include_dir=args.onnxruntime_include_dir,
            expected_commit=args.expected_commit,
        )

        if not args.rebuild and _is_current(destination, facts):
            print(f"sherpa-onnx is already installed and current: {destination}")
            if not args.skip_verify:
                print("== verify ==")
                print(_verify(destination, python))
            _print_runtime_paths(destination)
            return 0

        environment_problems, warnings = _environment_problems(
            facts,
            build_dir=build_dir,
            destination=destination,
            min_free_gib=args.min_free_gib,
        )
        print("== preflight ==")
        _report(environment_problems, warnings, "preflight")

        _build_and_install(
            facts=facts,
            build_dir=build_dir,
            jobs=args.jobs,
            destination=destination,
            rebuild=args.rebuild,
            skip_verify=args.skip_verify,
        )
        print(f"sherpa-onnx {PINNED_VERSION} installed for {facts['python_tag']}")
        _print_runtime_paths(destination)
        return 0
    except (InstallError, OSError) as exc:
        print(f"install failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
