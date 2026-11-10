import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from robotsoul.build import (
    BuildError,
    BuildResult,
    available_sdk_targets,
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

            self.assertEqual(len(commands), 3)
            self.assertTrue(str(root / "build") in " ".join(commands[0]))
            self.assertTrue(str(root / "build") in " ".join(commands[1]))
            self.assertTrue(str(root / "build") in " ".join(commands[2]))
            self.assertEqual(len(result.binaries), 7)
            self.assertTrue(all(path.is_file() for path in result.binaries))
            self.assertTrue(all(path.is_relative_to(root / "build") for path in result.binaries))
            self.assertEqual(result.binaries[-1].name, "picoclaw")
            self.assertEqual(go_environment["GOOS"], "linux")
            self.assertEqual(go_environment["GOARCH"], "amd64")
            self.assertEqual(go_environment["CGO_ENABLED"], "0")
            self.assertTrue(Path(go_environment["GOCACHE"]).is_relative_to(root / "build"))
            self.assertTrue(Path(go_environment["GOMODCACHE"]).is_relative_to(root / "build"))
            self.assertEqual((picoclaw / "build").exists(), False)
            self.assertEqual(len(result.staged_dependencies), 3)
            manifest = json.loads(result.manifest.read_text(encoding="utf-8"))
            self.assertEqual(manifest["architecture"], "x86_64")
            self.assertEqual(manifest["build_type"], "Release")
            self.assertIn("picoclaw", manifest["targets"])
            configure = commands[0]
            self.assertIn(f"-DCMAKE_BUILD_TYPE=Release", configure)
            self.assertIn("-B", configure)
            self.assertTrue(Path(configure[configure.index("-B") + 1]).is_relative_to(root / "build"))
            self.assertIn("--parallel", commands[1])

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

    def test_dirty_picoclaw_module_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            module_dir = _write_picoclaw_module(Path(temporary))
            with patch("robotsoul.build.subprocess.run") as run:
                run.return_value.returncode = 0
                run.return_value.stdout = " M go.mod\n"
                with self.assertRaisesRegex(BuildError, "go.mod/go.sum contain local changes"):
                    build_module._validate_picoclaw_go_module(module_dir)

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
        sdk_root / "thirdparty" / "lib" / architecture / "libddscxx.so",
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


if __name__ == "__main__":
    unittest.main()
