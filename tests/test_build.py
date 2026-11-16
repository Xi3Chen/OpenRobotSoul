import json
import os
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from robotsoul.build import (
    BuildError,
    BuildResult,
    available_sdk_targets,
    build_dialogue,
    build_project,
    detect_host_architecture,
    detect_host_platform,
    main,
    normalize_architecture,
    normalize_platform,
)
from robotsoul import build as build_module


class BuildToolTests(unittest.TestCase):
    def test_platform_and_architecture_aliases_are_normalized(self):
        self.assertEqual(normalize_platform("Win32"), "windows")
        self.assertEqual(normalize_platform("Darwin"), "macos")
        self.assertEqual(normalize_architecture("AMD64"), "x86_64")
        self.assertEqual(normalize_architecture("arm64"), "aarch64")

    def test_host_detection_maps_python_platform_values(self):
        self.assertEqual(detect_host_platform("win32"), "windows")
        self.assertEqual(detect_host_platform("darwin"), "macos")
        self.assertEqual(detect_host_platform("linux"), "linux")
        self.assertEqual(detect_host_architecture("AMD64"), "x86_64")

    def test_available_targets_reflect_sdk_library_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for architecture in ("x86_64", "aarch64"):
                _write_sdk_libraries(root, architecture)

            self.assertEqual(
                available_sdk_targets(root),
                (("linux", "x86_64"), ("linux", "aarch64")),
            )

    def test_build_stages_all_outputs_inside_root_build_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_sdk_libraries(root, "x86_64")
            picoclaw = _write_picoclaw_module(root)
            _write_dialogue_module(root)
            commands = []
            go_environment = {}

            def runner(command, _cwd):
                commands.append(list(command))
                if "build" in command and command[0].endswith("go"):
                    go_environment.update(
                        {name: build_module.os.environ.get(name) for name in ("GOOS", "GOARCH", "CGO_ENABLED", "GOCACHE", "GOMODCACHE")}
                    )
                    output = Path(command[command.index("-o") + 1])
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_bytes(b"picoclaw-test-binary")
                if "--build" in command:
                    build_dir = Path(command[command.index("--build") + 1])
                    bin_dir = build_dir / "bin"
                    bin_dir.mkdir(parents=True, exist_ok=True)
                    for target in command[command.index("--target") + 1:]:
                        (bin_dir / target).write_bytes(b"test-binary")
                    if "unitree_go2_bridge" in command:
                        output = Path(command[command.index("--build") + 1]).parent / "lib"
                        output.mkdir(parents=True, exist_ok=True)
                        (output / "libopenrobotsoul_go2_bridge.so").write_bytes(b"test-bridge")

            with patch("robotsoul.build._validate_pico_go_version"), patch(
                "robotsoul.build._validate_picoclaw_go_module"
            ), patch("robotsoul.build._picoclaw_ldflags", return_value=""):
                result = build_project(
                    platform_name="linux",
                    architecture="amd64",
                    project_root=root,
                    which=lambda name: f"/tool/{name}" if name in {"cmake", "c++", "go"} else None,
                    runner=runner,
                    host_system="linux",
                    host_machine="x86_64",
                    jobs=3,
                )

            self.assertEqual(len(commands), 6)
            self.assertTrue(str(root / "build") in " ".join(commands[0]))
            self.assertTrue(str(root / "build") in " ".join(commands[1]))
            self.assertTrue(str(root / "build") in " ".join(commands[2]))
            self.assertTrue(str(root / "build") in " ".join(commands[3]))
            self.assertTrue(str(root / "build") in " ".join(commands[4]))
            self.assertTrue(str(root / "build") in " ".join(commands[5]))
            self.assertEqual(len(result.binaries), 8)
            self.assertTrue(all(path.is_file() for path in result.binaries))
            self.assertTrue(all(path.is_relative_to(root / "build") for path in result.binaries))
            self.assertEqual(result.binaries[-2].name, "picoclaw")
            self.assertEqual(result.binaries[-1].name, "openrobotsoul-dialogue")
            self.assertEqual(go_environment["GOOS"], "linux")
            self.assertEqual(go_environment["GOARCH"], "amd64")
            self.assertEqual(go_environment["CGO_ENABLED"], "0")
            self.assertTrue(Path(go_environment["GOCACHE"]).is_relative_to(root / "build"))
            self.assertTrue(Path(go_environment["GOMODCACHE"]).is_relative_to(root / "build"))
            self.assertEqual((picoclaw / "build").exists(), False)
            self.assertEqual(len(result.staged_dependencies), 6)
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["architecture"], "x86_64")
            self.assertEqual(manifest["build_type"], "Release")
            self.assertIn("picoclaw", manifest["targets"])
            self.assertIn("openrobotsoul-dialogue", manifest["targets"])
            self.assertEqual(manifest["picoclaw_commit"], build_module.EXPECTED_PICOCLAW_COMMIT)
            configure = commands[0]
            self.assertIn(f"-DCMAKE_BUILD_TYPE=Release", configure)
            self.assertIn("-B", configure)
            self.assertTrue(Path(configure[configure.index("-B") + 1]).is_relative_to(root / "build"))
            self.assertIn("--parallel", commands[1])
            self.assertIn("unitree_go2_bridge", commands[3])

    def test_unsupported_sdk_platform_fails_with_available_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_sdk_libraries(root, "x86_64")

            with self.assertRaisesRegex(BuildError, "does not include libraries for windows/x86_64"):
                build_project(
                    platform_name="windows",
                    architecture="x86_64",
                    project_root=root,
                    host_system="windows",
                    host_machine="AMD64",
                )

    def test_cross_compile_requires_a_real_toolchain_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_sdk_libraries(root, "aarch64")

            with self.assertRaisesRegex(BuildError, "provide --toolchain"):
                build_project(
                    platform_name="linux",
                    architecture="aarch64",
                    project_root=root,
                    host_system="linux",
                    host_machine="x86_64",
                )

    def test_missing_cmake_reports_platform_dependency_guidance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_sdk_libraries(root, "x86_64")

            with self.assertRaisesRegex(BuildError, "apt-get install"):
                build_project(
                    platform_name="linux",
                    architecture="x86_64",
                    project_root=root,
                    which=lambda name: "/tool/go" if name == "go" else None,
                    host_system="linux",
                    host_machine="x86_64",
                )

    def test_missing_go_reports_pico_requirement_before_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_sdk_libraries(root, "x86_64")
            _write_picoclaw_module(root)
            with self.assertRaisesRegex(BuildError, "Go 1.25.13 or newer"):
                build_project(
                    platform_name="linux",
                    architecture="x86_64",
                    project_root=root,
                    which=lambda name: f"/tool/{name}" if name in {"cmake", "c++"} else None,
                    host_system="linux",
                    host_machine="x86_64",
                )

    def test_old_go_version_reports_required_and_found_versions(self):
        with patch("robotsoul.build._go_version", return_value=(1, 24, 6)):
            with self.assertRaisesRegex(BuildError, "requires Go 1.25.13 or newer.*found Go 1.24.6"):
                build_module._validate_pico_go_version(Path("."), lambda _name: "go")

    def test_dialogue_builds_native_windows_and_cross_compiled_linux_without_cpp_tools(self):
        for platform_name, architecture, canonical_arch, binary_name in (
            ("windows", "amd64", "x86_64", "openrobotsoul-dialogue.exe"),
            ("linux", "amd64", "x86_64", "openrobotsoul-dialogue"),
            ("linux", "arm64", "aarch64", "openrobotsoul-dialogue"),
        ):
            with self.subTest(platform=platform_name, architecture=architecture), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_picoclaw_module(root)
                module_dir = _write_dialogue_module(root)
                output_dir = root / "build" / f"{platform_name}-{canonical_arch}"
                output_dir.mkdir(parents=True)
                full_manifest = output_dir / "manifest.json"
                full_manifest.write_text("full-build-manifest", encoding="utf-8")
                commands = []
                environments = []
                lookups = []
                original_environment = {name: os.environ.get(name) for name in ("GOOS", "GOARCH", "GOTOOLCHAIN", "GOWORK")}

                def which(name):
                    lookups.append(name)
                    return "/tool/go" if name == "go" else None

                def runner(command, cwd):
                    commands.append((list(command), cwd))
                    environments.append(dict(os.environ))
                    Path(command[command.index("-o") + 1]).write_bytes(b"dialogue-test-binary")

                with patch("robotsoul.build._go_version", return_value=(1, 25, 13)), patch(
                    "robotsoul.build._validate_picoclaw_go_module"
                ), patch("robotsoul.build._picoclaw_ldflags", return_value="test-flags"):
                    result = build_dialogue(
                        platform_name=platform_name,
                        architecture=architecture,
                        project_root=root,
                        which=which,
                        runner=runner,
                        jobs=2,
                    )

                self.assertEqual(lookups, ["go"])
                self.assertEqual(len(commands), 1)
                command, cwd = commands[0]
                self.assertEqual(cwd, module_dir)
                self.assertEqual(command[command.index("-tags") + 1], "goolm,stdjson")
                self.assertIn("-mod=readonly", command)
                self.assertEqual(command[command.index("-p") + 1], "2")
                self.assertEqual(result.binaries, (output_dir / "bin" / binary_name,))
                self.assertTrue(result.binaries[0].is_file())
                self.assertEqual(result.staged_dependencies, ())
                environment = environments[0]
                self.assertEqual(environment["GOOS"], platform_name)
                self.assertEqual(environment["GOARCH"], architecture)
                self.assertEqual(environment["CGO_ENABLED"], "0")
                self.assertEqual(environment["GOTOOLCHAIN"], "local")
                self.assertEqual(environment["GOWORK"], "off")
                for variable in ("GOCACHE", "GOMODCACHE"):
                    self.assertTrue(Path(environment[variable]).is_relative_to(root / "build"))
                self.assertEqual({name: os.environ.get(name) for name in original_environment}, original_environment)
                self.assertEqual(full_manifest.read_text(encoding="utf-8"), "full-build-manifest")
                self.assertEqual(result.manifest.name, "dialogue-manifest.json")
                manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
                self.assertEqual(manifest["targets"], ["openrobotsoul-dialogue"])
                self.assertEqual(manifest["picoclaw_commit"], build_module.EXPECTED_PICOCLAW_COMMIT)

    def test_dialogue_requires_go_without_checking_cpp_dependencies(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(BuildError, "Go 1.25.13 or newer"):
                build_dialogue(
                    platform_name="windows",
                    architecture="amd64",
                    project_root=Path(temporary),
                    which=lambda _name: None,
                )

    def test_dialogue_missing_module_does_not_start_build(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_picoclaw_module(root)
            with patch("robotsoul.build._validate_pico_go_version", return_value="go"), patch(
                "robotsoul.build._validate_picoclaw_go_module"
            ):
                with self.assertRaisesRegex(BuildError, "Dialogue Go module is missing"):
                    build_dialogue(platform_name="linux", architecture="amd64", project_root=root)

    def test_dialogue_does_not_report_success_when_build_fails_or_artifact_is_missing(self):
        for build_failure in (True, False):
            with self.subTest(build_failure=build_failure), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _write_picoclaw_module(root)
                _write_dialogue_module(root)

                def runner(_command, _cwd):
                    if build_failure:
                        raise BuildError("compiler error")

                with patch("robotsoul.build._validate_pico_go_version", return_value="go"), patch(
                    "robotsoul.build._validate_picoclaw_go_module"
                ), patch("robotsoul.build._picoclaw_ldflags", return_value=""):
                    expected = "Dialogue build failed" if build_failure else "without producing"
                    with self.assertRaisesRegex(BuildError, expected):
                        build_dialogue(platform_name="linux", architecture="amd64", project_root=root, runner=runner)
                self.assertFalse((root / "build" / "linux-x86_64" / "dialogue-manifest.json").exists())

    def test_dialogue_rejects_invalid_targets_and_jobs(self):
        for arguments, expected in (
            ({"platform_name": "unknown", "architecture": "amd64"}, "Unsupported platform"),
            ({"platform_name": "linux", "architecture": "armv7"}, "Unsupported architecture"),
            ({"platform_name": "linux", "architecture": "amd64", "jobs": 0}, "positive integer"),
        ):
            with self.subTest(arguments=arguments), self.assertRaisesRegex(BuildError, expected):
                build_dialogue(**arguments)

    def test_dirty_picoclaw_sources_and_untracked_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            module_dir = _write_picoclaw_module(Path(temporary))
            for status in (" M go.mod\n", " M pkg/agent/loop.go\n", "?? scratch.go\n"):
                with self.subTest(status=status), patch(
                    "robotsoul.build.subprocess.run", return_value=CompletedProcess([], 0, status, "")
                ):
                    with self.assertRaisesRegex(BuildError, "local changes or untracked files"):
                        build_module._validate_picoclaw_go_module(module_dir)

    def test_picoclaw_wrong_commit_is_rejected_even_when_clean(self):
        with patch("robotsoul.build.subprocess.run", side_effect=(
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, "a" * 40 + "\n", ""),
        )):
            with self.assertRaisesRegex(BuildError, f"verified commit {build_module.EXPECTED_PICOCLAW_COMMIT}"):
                build_module._validate_picoclaw_go_module(Path("picoclaw"))

    def test_clean_verified_picoclaw_checkout_is_accepted(self):
        with patch("robotsoul.build.subprocess.run", side_effect=(
            CompletedProcess([], 0, "", ""),
            CompletedProcess([], 0, build_module.EXPECTED_PICOCLAW_COMMIT + "\n", ""),
        )) as run:
            build_module._validate_picoclaw_go_module(Path("picoclaw"))
        status_command = run.call_args_list[0].args[0]
        self.assertIn("--untracked-files=all", status_command)
        self.assertNotIn("go.mod", status_command)

    def test_picoclaw_git_validation_failures_are_reported(self):
        for responses, expected in (
            ((CompletedProcess([], 1, "", "not a repository"),), "working tree"),
            ((CompletedProcess([], 0, "", ""), CompletedProcess([], 1, "", "bad revision")), "read PicoClaw commit"),
        ):
            with self.subTest(expected=expected), patch("robotsoul.build.subprocess.run", side_effect=responses):
                with self.assertRaisesRegex(BuildError, expected):
                    build_module._validate_picoclaw_go_module(Path("picoclaw"))
        with patch("robotsoul.build.subprocess.run", side_effect=FileNotFoundError("git")):
            with self.assertRaisesRegex(BuildError, "using Git"):
                build_module._validate_picoclaw_go_module(Path("picoclaw"))

    def test_dialogue_cli_skips_sdk_wizard_and_defaults_to_host(self):
        result = BuildResult("windows", "x86_64", "Release", Path("build/windows-x86_64"), (), (), Path("build/windows-x86_64/dialogue-manifest.json"))
        with patch("robotsoul.build.detect_host_platform", return_value="windows"), patch(
            "robotsoul.build.detect_host_architecture", return_value="x86_64"
        ), patch("robotsoul.build._interactive_options") as wizard, patch(
            "robotsoul.build.build_dialogue", return_value=result
        ) as build, patch("robotsoul.build.build_project") as full_build:
            self.assertEqual(main(["--dialogue-only", "--jobs", "2"]), 0)
        wizard.assert_not_called()
        full_build.assert_not_called()
        build.assert_called_once_with(platform_name="windows", architecture="x86_64", build_type="Release", jobs=2)

    def test_dialogue_cli_supports_explicit_cross_compilation(self):
        result = BuildResult("linux", "aarch64", "Release", Path("build/linux-aarch64"), (), (), Path("build/linux-aarch64/dialogue-manifest.json"))
        with patch("robotsoul.build.build_dialogue", return_value=result) as build:
            self.assertEqual(main(["--dialogue-only", "--platform", "linux", "--arch", "arm64"]), 0)
        build.assert_called_once_with(platform_name="linux", architecture="aarch64", build_type="Release", jobs=None)

    def test_dialogue_cli_guide_and_targets_need_no_sdk(self):
        with patch("robotsoul.build.available_sdk_targets") as sdk, patch("robotsoul.build.build_dialogue") as build, patch("builtins.print") as output:
            self.assertEqual(main(["--dialogue-only", "--list-targets"]), 0)
            self.assertIn("linux/aarch64", str(output.call_args_list))
            self.assertIn("windows/x86_64", str(output.call_args_list))
            self.assertEqual(main(["--dialogue-only", "--guide"]), 0)
        sdk.assert_not_called()
        build.assert_not_called()

    def test_dialogue_cli_rejects_cmake_toolchain_and_reports_build_errors(self):
        with patch("builtins.print"), patch("robotsoul.build.build_dialogue") as build:
            self.assertEqual(main(["--dialogue-only", "--toolchain", "unused.cmake"]), 2)
        build.assert_not_called()
        with patch("builtins.print"), patch("robotsoul.build.build_dialogue", side_effect=BuildError("compiler error")):
            self.assertEqual(main(["--dialogue-only"]), 1)

    def test_no_arguments_use_interactive_defaults_then_build(self):
        with patch("robotsoul.build.detect_host_platform", return_value="linux"), patch(
            "robotsoul.build.detect_host_architecture", return_value="x86_64"
        ), patch(
            "robotsoul.build.available_sdk_targets", return_value=(("linux", "x86_64"),)
        ), patch(
            "robotsoul.build.dependency_status",
            return_value={"cmake": "/tools/cmake", "cxx_compiler": "/tools/c++"},
        ), patch("builtins.input", side_effect=("", "", "")), patch(
            "robotsoul.build.build_project",
            return_value=BuildResult("linux", "x86_64", "Release", Path("build/linux-x86_64"), (), (), Path("build/linux-x86_64/manifest.json")),
        ) as build:
            self.assertEqual(main([]), 0)

        build.assert_called_once_with(
            platform_name="linux",
            architecture="x86_64",
            build_type="Release",
            toolchain=None,
            jobs=None,
        )

    def test_no_arguments_without_interactive_input_prints_cli_guidance(self):
        with patch("robotsoul.build.detect_host_platform", return_value="linux"), patch(
            "robotsoul.build.detect_host_architecture", return_value="x86_64"
        ), patch("builtins.input", side_effect=EOFError), patch("builtins.print") as output:
            self.assertEqual(main([]), 2)

        self.assertIn("--platform and --arch", str(output.call_args_list))


def _write_sdk_libraries(root: Path, architecture: str) -> None:
    sdk_root = root / "adapters" / "unitree_sdk2"
    (sdk_root / "CMakeLists.txt").parent.mkdir(parents=True, exist_ok=True)
    (sdk_root / "CMakeLists.txt").write_text("project(test_sdk)\n", encoding="utf-8")
    paths = (
        sdk_root / "lib" / architecture / "libunitree_sdk2.a",
        sdk_root / "thirdparty" / "lib" / architecture / "libddsc.so",
        sdk_root / "thirdparty" / "lib" / architecture / "libddsc.so.0",
        sdk_root / "thirdparty" / "lib" / architecture / "libddscxx.so",
        sdk_root / "thirdparty" / "lib" / architecture / "libddscxx.so.0",
    )
    for path in paths:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"sdk-library")


def _write_picoclaw_module(root: Path) -> Path:
    module_dir = root / "third_party" / "picoclaw"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "go.mod").write_text("module github.com/sipeed/picoclaw\ngo 1.25.13\n", encoding="utf-8")
    (module_dir / ".git").mkdir()
    return module_dir


def _write_dialogue_module(root: Path) -> Path:
    module_dir = root / "cmd" / "openrobotsoul-dialogue"
    module_dir.mkdir(parents=True, exist_ok=True)
    (module_dir / "go.mod").write_text("module openrobotsoul-dialogue\ngo 1.25.13\n", encoding="utf-8")
    return module_dir


if __name__ == "__main__":
    unittest.main()
