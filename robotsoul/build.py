from __future__ import annotations

import argparse
import json
import os
import platform as host_platform
import shutil
import subprocess
import sys
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = Path("adapters") / "unitree_sdk2"
PICOCLAW_ROOT = Path("third_party") / "picoclaw"
GO2_TARGETS = (
    "go2_sport_client",
    "go2_low_level",
    "go2_stand_example",
    "go2_robot_state_client",
    "go2_video_client",
    "go2_vui_client",
)
PICOCLAW_MODULE = "./cmd/picoclaw"

PLATFORM_ALIASES = {
    "linux": "linux",
    "ubuntu": "linux",
    "debian": "linux",
    "win": "windows",
    "win32": "windows",
    "windows": "windows",
    "darwin": "macos",
    "mac": "macos",
    "macos": "macos",
    "osx": "macos",
}
ARCH_ALIASES = {
    "amd64": "x86_64",
    "x64": "x86_64",
    "x86_64": "x86_64",
    "arm64": "aarch64",
    "aarch64": "aarch64",
}

CommandRunner = Callable[[Sequence[str], Path], None]


class BuildError(RuntimeError):
    pass


@dataclass(frozen=True)
class BuildResult:
    platform: str
    architecture: str
    build_type: str
    output_dir: Path
    binaries: tuple[Path, ...]
    staged_dependencies: tuple[Path, ...]
    manifest: Path


def normalize_platform(value: str) -> str:
    try:
        return PLATFORM_ALIASES[value.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise BuildError(f"Unsupported platform {value!r}; choose linux, windows, or macos") from exc


def normalize_architecture(value: str) -> str:
    try:
        return ARCH_ALIASES[value.strip().lower()]
    except (AttributeError, KeyError) as exc:
        raise BuildError(f"Unsupported architecture {value!r}; choose x86_64 or aarch64") from exc


def detect_host_platform(system: str | None = None) -> str:
    value = (system or sys.platform).lower()
    if value.startswith("linux"):
        return "linux"
    if value.startswith("win") or value == "cygwin":
        return "windows"
    if value == "darwin":
        return "macos"
    raise BuildError(f"Cannot identify the host platform from {value!r}")


def detect_host_architecture(machine: str | None = None) -> str:
    value = (machine or host_platform.machine()).lower()
    try:
        return normalize_architecture(value)
    except BuildError as exc:
        raise BuildError(
            f"Host architecture {value!r} is not supported by this build tool; use x86_64 or aarch64"
        ) from exc


def available_sdk_targets(project_root: Path = PROJECT_ROOT) -> tuple[tuple[str, str], ...]:
    sdk = project_root / SDK_ROOT
    targets = []
    for architecture in ("x86_64", "aarch64"):
        required = (
            sdk / "lib" / architecture / "libunitree_sdk2.a",
            sdk / "thirdparty" / "lib" / architecture / "libddsc.so",
            sdk / "thirdparty" / "lib" / architecture / "libddscxx.so",
        )
        if all(path.is_file() for path in required):
            targets.append(("linux", architecture))
    return tuple(targets)


def dependency_status(platform_name: str, which: Callable[[str], str | None] = shutil.which) -> dict[str, str | None]:
    compiler_candidates = {
        "linux": ("c++", "g++", "clang++"),
        "windows": ("cl", "clang++", "g++"),
        "macos": ("c++", "clang++", "g++"),
    }[platform_name]
    compiler = next((path for name in compiler_candidates if (path := which(name))), None)
    return {"cmake": which("cmake"), "cxx_compiler": compiler, "go": which("go")}


def _go_version(go: str, project_root: Path) -> tuple[int, int, int]:
    try:
        completed = subprocess.run([go, "version"], cwd=project_root, check=True, capture_output=True, text=True)
        result = completed.stdout.strip() or completed.stderr.strip()
    except FileNotFoundError as exc:
        raise BuildError(f"Go compiler was not found at {go}; install Go 1.25.13 or newer") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"Unable to query Go compiler version using {go}: {exc}") from exc
    match = re.search(r"go(\d+)\.(\d+)(?:\.(\d+))?", result)
    if not match:
        raise BuildError(f"Unable to parse Go compiler version: {result!r}")
    return tuple(int(part or 0) for part in match.groups())


@contextmanager
def _temporary_environment(values: dict[str, str]):
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def dependency_guidance(platform_name: str) -> tuple[str, ...]:
    if platform_name == "linux":
        return (
            "PicoClaw requires Go 1.25.13 or newer: https://go.dev/dl/",
            "Ubuntu/Debian: sudo apt-get update",
            "Ubuntu/Debian: sudo apt-get install -y cmake build-essential libyaml-cpp-dev libeigen3-dev libboost-all-dev libfmt-dev",
            "Fedora: sudo dnf install cmake gcc-c++ yaml-cpp-devel eigen3-devel boost-devel fmt-devel",
            "Arch Linux: sudo pacman -S cmake base-devel yaml-cpp eigen boost fmt",
        )
    if platform_name == "macos":
        return (
            "PicoClaw requires Go 1.25.13 or newer: https://go.dev/dl/",
            "Install Apple Command Line Tools: xcode-select --install",
            "Install CMake and SDK dependencies: brew install cmake ninja yaml-cpp eigen boost fmt",
            "Note: the checked-in Unitree SDK libraries currently support Linux only.",
        )
    return (
        "PicoClaw requires Go 1.25.13 or newer: https://go.dev/dl/",
        "Install CMake: winget install --id Kitware.CMake --exact",
        "Install a C++ compiler: winget install --id Microsoft.VisualStudio.2022.BuildTools --exact",
        "In Visual Studio Installer, add the Desktop development with C++ workload.",
        "Note: the checked-in Unitree SDK libraries currently support Linux only.",
    )


def _default_runner(command: Sequence[str], cwd: Path) -> None:
    try:
        subprocess.run(list(command), cwd=cwd, check=True)
    except FileNotFoundError as exc:
        raise BuildError(f"Build command was not found: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        raise BuildError(f"Build command failed with exit code {exc.returncode}: {command[0]}") from exc


def build_project(
    *,
    platform_name: str,
    architecture: str,
    build_type: str = "Release",
    toolchain: str | Path | None = None,
    jobs: int | None = None,
    project_root: Path = PROJECT_ROOT,
    which: Callable[[str], str | None] = shutil.which,
    runner: CommandRunner = _default_runner,
    host_system: str | None = None,
    host_machine: str | None = None,
) -> BuildResult:
    target_platform = normalize_platform(platform_name)
    target_arch = normalize_architecture(architecture)
    build_types = {
        "debug": "Debug",
        "release": "Release",
        "relwithdebinfo": "RelWithDebInfo",
        "minsizerel": "MinSizeRel",
    }
    try:
        normalized_build_type = build_types[build_type.replace("_", "").lower()]
    except (AttributeError, KeyError) as exc:
        raise BuildError("Build type must be Debug, Release, RelWithDebInfo, or MinSizeRel")
    if jobs is not None and jobs < 1:
        raise BuildError("Build jobs must be a positive integer")

    project_root = project_root.resolve()
    sdk_dir = project_root / SDK_ROOT
    picoclaw_dir = project_root / PICOCLAW_ROOT
    if not (sdk_dir / "CMakeLists.txt").is_file():
        raise BuildError(f"Unitree SDK CMake project is missing: {sdk_dir}")
    available = available_sdk_targets(project_root)
    target = (target_platform, target_arch)
    if target not in available:
        choices = ", ".join(f"{os_name}/{arch}" for os_name, arch in available) or "none"
        raise BuildError(
            f"Unitree SDK does not include libraries for {target_platform}/{target_arch}. "
            f"Targets available in this checkout: {choices}."
        )

    host = detect_host_platform(host_system)
    host_arch = detect_host_architecture(host_machine)
    toolchain_path = Path(toolchain).resolve() if toolchain else None
    if toolchain_path is not None and not toolchain_path.is_file():
        raise BuildError(f"CMake toolchain file does not exist: {toolchain_path}")
    if target != (host, host_arch) and toolchain_path is None:
        raise BuildError(
            f"Target {target_platform}/{target_arch} differs from host {host}/{host_arch}; "
            "provide --toolchain for cross compilation."
        )

    dependencies = dependency_status(host, which)
    if dependencies["go"] is None:
        raise BuildError(
            "Missing development component: Go compiler. PicoClaw go.mod requires Go 1.25.13 or newer. "
            "Install it from https://go.dev/dl/ and retry."
        )
    missing = [name for name, path in dependencies.items() if path is None]
    if missing:
        guidance = "\n".join(f"  {item}" for item in dependency_guidance(host))
        raise BuildError(
            f"Missing build dependencies: {', '.join(missing)}. Install them, then retry.\n{guidance}"
        )

    _validate_pico_go_version(project_root, which)
    picoclaw_mod = picoclaw_dir / "go.mod"
    if not picoclaw_mod.is_file():
        raise BuildError(f"PicoClaw Go module is missing: {picoclaw_mod}; initialize the Git submodule first")
    _validate_picoclaw_go_module(picoclaw_dir)

    output_dir = project_root / "build" / f"{target_platform}-{target_arch}"
    cmake_build_dir = output_dir / "cmake"
    go_build_dir = output_dir / "go"
    binary_dir = output_dir / "bin"
    dependency_dir = output_dir / "lib"
    binary_dir.mkdir(parents=True, exist_ok=True)
    dependency_dir.mkdir(parents=True, exist_ok=True)
    go_cache_dir = go_build_dir / "cache"
    go_module_cache_dir = go_build_dir / "modules"
    go_build_cache_dir = go_build_dir / "gocache"
    for directory in (go_cache_dir, go_module_cache_dir, go_build_cache_dir):
        directory.mkdir(parents=True, exist_ok=True)

    sdk_arch_lib = sdk_dir / "thirdparty" / "lib" / target_arch
    staged_dependencies: list[Path] = []
    for name in ("libddsc.so", "libddscxx.so"):
        source = sdk_arch_lib / name
        destination = dependency_dir / name
        if not source.is_file():
            raise BuildError(f"Required Unitree runtime dependency is missing: {source}")
        shutil.copy2(source, destination)
        staged_dependencies.append(destination)

    sdk_static_library = sdk_dir / "lib" / target_arch / "libunitree_sdk2.a"
    static_destination = dependency_dir / sdk_static_library.name
    shutil.copy2(sdk_static_library, static_destination)
    staged_dependencies.append(static_destination)

    configure_command = [
        dependencies["cmake"],
        "-S",
        str(sdk_dir),
        "-B",
        str(cmake_build_dir),
        "-DBUILD_EXAMPLES=ON",
        f"-DCMAKE_BUILD_TYPE={normalized_build_type}",
        f"-DCMAKE_SYSTEM_PROCESSOR={target_arch}",
        f"-DCMAKE_BUILD_RPATH={dependency_dir}",
        "-DCMAKE_INSTALL_RPATH=$ORIGIN/../lib",
    ]
    if toolchain_path is not None:
        configure_command.append(f"-DCMAKE_TOOLCHAIN_FILE={toolchain_path}")
    runner(configure_command, project_root)

    build_command = [
        dependencies["cmake"],
        "--build",
        str(cmake_build_dir),
        "--config",
        normalized_build_type,
        "--target",
        *GO2_TARGETS,
    ]
    if jobs is not None:
        build_command.extend(("--parallel", str(jobs)))
    runner(build_command, project_root)

    staged_binaries = []
    for target_name in GO2_TARGETS:
        matches = tuple(cmake_build_dir.rglob(target_name)) + tuple(cmake_build_dir.rglob(f"{target_name}.exe"))
        candidates = [path for path in matches if path.is_file()]
        if not candidates:
            raise BuildError(f"Build completed but did not produce expected target {target_name}")
        source = max(candidates, key=lambda path: path.stat().st_mtime_ns)
        destination = binary_dir / source.name
        shutil.copy2(source, destination)
        staged_binaries.append(destination)

    goarch = {"x86_64": "amd64", "aarch64": "arm64"}[target_arch]
    goos = {"linux": "linux", "windows": "windows", "macos": "darwin"}[target_platform]
    picoclaw_name = "picoclaw.exe" if goos == "windows" else "picoclaw"
    picoclaw_binary = binary_dir / picoclaw_name
    go_environment = {
        "GOOS": goos,
        "GOARCH": goarch,
        "CGO_ENABLED": "0",
        "GOTOOLCHAIN": "local",
        "GOCACHE": str(go_build_cache_dir),
        "GOMODCACHE": str(go_module_cache_dir),
    }
    go_command = [
        dependencies["go"],
        "build",
        "-tags",
        "goolm,stdjson",
        "-mod=readonly",
        "-ldflags",
        _picoclaw_ldflags(picoclaw_dir, dependencies["go"]),
        "-o",
        str(picoclaw_binary),
        PICOCLAW_MODULE,
    ]
    try:
        with _temporary_environment(go_environment):
            runner(go_command, picoclaw_dir)
    except BuildError as exc:
        raise BuildError(f"PicoClaw build failed: {exc}") from exc
    if not picoclaw_binary.is_file():
        raise BuildError(f"PicoClaw build completed without producing {picoclaw_binary}")
    staged_binaries.append(picoclaw_binary)

    manifest_path = output_dir / "manifest.json"
    manifest = {
        "platform": target_platform,
        "architecture": target_arch,
        "build_type": normalized_build_type,
        "source": [str(SDK_ROOT), str(PICOCLAW_ROOT)],
        "targets": [*GO2_TARGETS, "picoclaw"],
        "binaries": [path.relative_to(project_root).as_posix() for path in staged_binaries],
        "dependencies": [path.relative_to(project_root).as_posix() for path in staged_dependencies],
        "toolchain": str(toolchain_path) if toolchain_path else None,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return BuildResult(
        target_platform,
        target_arch,
        normalized_build_type,
        output_dir,
        tuple(staged_binaries),
        tuple(staged_dependencies),
        manifest_path,
    )


def _picoclaw_ldflags(picoclaw_dir: Path, go: str) -> str:
    def git_value(*arguments: str) -> str:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=picoclaw_dir,
            check=False,
            capture_output=True,
            text=True,
        )
        return completed.stdout.strip() if completed.returncode == 0 else "dev"

    version = git_value("describe", "--tags", "--always", "--dirty")
    commit = git_value("rev-parse", "--short=8", "HEAD")
    version_result = subprocess.run([go, "env", "GOVERSION"], cwd=picoclaw_dir, check=False, capture_output=True, text=True)
    go_version = version_result.stdout.strip() if version_result.returncode == 0 else "unknown"
    build_time = datetime.now().astimezone().isoformat(timespec="seconds")
    package = "github.com/sipeed/picoclaw/pkg/config"
    return (
        f"-X {package}.Version={version} "
        f"-X {package}.GitCommit={commit} "
        f"-X {package}.BuildTime={build_time} "
        f"-X {package}.GoVersion={go_version} -s -w"
    )


def _validate_picoclaw_go_module(picoclaw_dir: Path) -> None:
    completed = subprocess.run(
        ["git", "status", "--short", "--", "go.mod", "go.sum"],
        cwd=picoclaw_dir,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise BuildError(f"Unable to validate PicoClaw module files: {completed.stderr.strip()}")
    if completed.stdout.strip():
        raise BuildError(
            "PicoClaw go.mod/go.sum contain local changes. Resolve or restore those dependency changes "
            "before building so the root build cache remains reproducible."
        )


def _validate_pico_go_version(project_root: Path, which: Callable[[str], str | None]) -> None:
    go = which("go")
    if go is None:
        raise BuildError(
            "Missing development component: Go compiler. PicoClaw go.mod requires Go 1.25.13 or newer. "
            "Install it from https://go.dev/dl/ and retry."
        )
    version = _go_version(go, project_root)
    if version < (1, 25, 13):
        found = ".".join(str(part) for part in version)
        raise BuildError(
            f"PicoClaw requires Go 1.25.13 or newer (go.mod declares go 1.25.13); found Go {found}. "
            "Install a compatible Go toolchain and retry."
        )


def _prompt_choice(label: str, choices: Sequence[str], default: str) -> str:
    while True:
        rendered = "/".join(choices)
        try:
            answer = input(f"{label} [{rendered}] (默认 {default}): ").strip()
        except EOFError as exc:
            raise BuildError(
                "Interactive input is unavailable; pass --platform and --arch explicitly."
            ) from exc
        if not answer:
            return default
        if answer.lower() in {choice.lower() for choice in choices}:
            return answer
        print(f"请输入其中一个选项：{rendered}")


def _interactive_options() -> dict[str, str | None]:
    try:
        default_platform = detect_host_platform()
        default_arch = detect_host_architecture()
    except BuildError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc

    print("OpenRobotSoul Go2 C++ 示例构建向导")
    print(f"构建文件、依赖副本和二进制将放在：{PROJECT_ROOT / 'build'}")
    print("当前 Unitree SDK checkout 只包含 Linux x86_64/aarch64 预编译库。")
    selected_platform = _prompt_choice("目标系统", ("linux", "windows", "macos"), default_platform)
    selected_arch = _prompt_choice("目标架构", ("x86_64", "aarch64"), default_arch)
    selected_build_type = _prompt_choice("构建类型", ("Release", "Debug"), "Release")
    target = (normalize_platform(selected_platform), normalize_architecture(selected_arch))
    if target not in available_sdk_targets():
        supported = ", ".join(f"{name}/{arch}" for name, arch in available_sdk_targets()) or "none"
        print(f"此 SDK checkout 没有 {target[0]}/{target[1]} 预编译库；可构建目标：{supported}")
        raise SystemExit(1)

    try:
        host = detect_host_platform()
        host_arch = detect_host_architecture()
        dependencies = dependency_status(host)
    except BuildError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(2) from exc
    missing = [name for name, path in dependencies.items() if path is None]
    if missing:
        print(f"宿主系统缺少构建依赖：{', '.join(missing)}")
        _print_dependency_help(host)
        try:
            answer = input("安装依赖后按 Enter 继续检查，输入 q 退出：").strip().lower()
        except EOFError:
            raise SystemExit(2) from None
        if answer == "q":
            raise SystemExit(2)
    toolchain = None
    if target != (host, host_arch):
        toolchain = input("目标与当前主机不同，请输入 CMake toolchain 文件路径（留空则稍后失败）：").strip() or None
    return {
        "platform": selected_platform,
        "arch": selected_arch,
        "build_type": selected_build_type,
        "toolchain": toolchain,
    }


def _print_dependency_help(platform_name: str) -> None:
    print(f"{platform_name} 构建依赖安装指引：")
    for instruction in dependency_guidance(platform_name):
        print(f"  {instruction}")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="robotsoul-build",
        description="跨平台配置并构建 Unitree Go2 SDK 示例；所有输出都位于项目根目录 build/。",
    )
    parser.add_argument("--platform", help="目标系统：linux、windows 或 macos")
    parser.add_argument("--arch", help="目标架构：x86_64/amd64 或 aarch64/arm64")
    parser.add_argument(
        "--build-type",
        default="Release",
        choices=("Debug", "Release", "RelWithDebInfo", "MinSizeRel"),
        help="CMake build type，默认 Release",
    )
    parser.add_argument("--toolchain", help="跨编译使用的 CMake toolchain 文件")
    parser.add_argument("--jobs", type=int, help="并行构建任务数")
    parser.add_argument("--guide", action="store_true", help="只输出目标平台依赖安装指引")
    parser.add_argument("--list-targets", action="store_true", help="显示此 SDK checkout 中可构建的目标平台/架构")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    if argv is None:
        provided_args = sys.argv[1:]
    else:
        provided_args = list(argv)

    if args.list_targets:
        targets = available_sdk_targets()
        if not targets:
            print("此 SDK checkout 中未找到支持平台的预编译库。")
            return 1
        for platform_name, architecture in targets:
            print(f"{platform_name}/{architecture}")
        return 0

    if not provided_args:
        try:
            choices = _interactive_options()
        except BuildError as exc:
            print(f"构建向导无法继续：{exc}", file=sys.stderr)
            return 2
        platform_name = str(choices["platform"])
        architecture = str(choices["arch"])
        build_type = str(choices["build_type"])
        toolchain = choices["toolchain"]
        if args.guide:
            _print_dependency_help(platform_name)
            return 0
    else:
        platform_name = args.platform
        architecture = args.arch
        build_type = args.build_type
        toolchain = args.toolchain
        if args.guide:
            selected_platform = normalize_platform(platform_name or detect_host_platform())
            _print_dependency_help(selected_platform)
            return 0
        if platform_name is None or architecture is None:
            print("参数模式必须同时提供 --platform 和 --arch。无参数运行可使用交互式构建向导。", file=sys.stderr)
            return 2

    try:
        result = build_project(
            platform_name=platform_name,
            architecture=architecture,
            build_type=build_type,
            toolchain=toolchain,
            jobs=args.jobs,
        )
    except BuildError as exc:
        print(f"构建失败：{exc}", file=sys.stderr)
        return 1

    print(f"构建完成：{result.output_dir}")
    print("二进制文件：")
    for binary in result.binaries:
        print(f"  {binary}")
    print(f"运行依赖：{result.output_dir / 'lib'}")
    print(f"构建清单：{result.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
