from __future__ import annotations

import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app_metadata
import build
import wavescout


class ReleaseReliabilityTests(unittest.TestCase):
    def test_version_file_is_authoritative(self) -> None:
        version = build.read_version(ROOT / "VERSION")
        self.assertEqual(version, app_metadata.APP_VERSION)
        self.assertEqual(wavescout.APP_VERSION, version)
        self.assertEqual(build.portable_zip_name(version), f"WaveScout_v{version}_Portable.zip")

    def test_runtime_requirements_do_not_include_pyinstaller(self) -> None:
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
        build_requirements = (ROOT / "requirements-build.txt").read_text(encoding="utf-8").lower()
        self.assertNotIn("pyinstaller", requirements)
        self.assertIn("pyinstaller", build_requirements)

    def test_spec_is_repo_relative_and_has_no_bytecode_fallback(self) -> None:
        spec = (ROOT / "WaveScout.spec").read_text(encoding="utf-8")
        self.assertNotIn(r"C:\Users\Josh", spec)
        self.assertNotIn("_missing_stdlib_pycs", spec)
        self.assertNotIn("stdlib_pyc", spec)
        self.assertNotIn("encodings_pyc", spec)
        self.assertNotIn("hooks/rthook_fix_encodings.py", spec)
        self.assertIn('str(ROOT / "wavescout.py")', spec)
        self.assertIn('str(ROOT / "VERSION")', spec)

    def test_build_script_filters_environment_specific_path_entries(self) -> None:
        original = os.pathsep.join(
            [
                r"C:\Windows\System32",
                r"C:\Users\Josh\.cache\codex-runtimes\poppler\bin",
                r"C:\Tools\Python311",
                r"C:\RandomDlls",
            ]
        )
        filtered = build.sanitized_path_entries(
            original,
            [Path(r"C:\Windows"), Path(r"C:\Tools\Python311")],
        )
        self.assertEqual(filtered, [r"C:\Windows\System32", r"C:\Tools\Python311"])

    def test_build_script_refuses_to_clean_repo_root(self) -> None:
        with self.assertRaises(ValueError):
            build._assert_within_repo(ROOT, ROOT)

    def test_build_script_requires_python_311(self) -> None:
        build.assert_python_311()

    def test_bundle_audit_rejects_unexpected_root_dll_and_dev_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "WaveScout"
            internal = app_dir / "_internal"
            internal.mkdir(parents=True)
            (app_dir / "WaveScout.exe").write_bytes(b"placeholder")
            (app_dir / "README.txt").write_text("readme", encoding="utf-8")
            (app_dir / "VERSION").write_text("1.0.0", encoding="utf-8")
            (app_dir / "icudt78.dll").write_bytes(b"unexpected")

            with self.assertRaisesRegex(RuntimeError, "root-level DLL"):
                build.validate_bundle(app_dir)

            (app_dir / "icudt78.dll").unlink()
            (internal / "build.py").write_text("unexpected", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "Forbidden bundled"):
                build.validate_bundle(app_dir)

    def test_bundle_audit_accepts_normal_onedir_shape(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            app_dir = Path(td) / "WaveScout"
            internal = app_dir / "_internal"
            internal.mkdir(parents=True)
            (app_dir / "WaveScout.exe").write_bytes(b"placeholder")
            (app_dir / "README.txt").write_text("readme", encoding="utf-8")
            (app_dir / "VERSION").write_text("1.0.0", encoding="utf-8")
            (internal / "Qt6Core.dll").write_bytes(b"qt dependency")

            build.validate_bundle(app_dir)

    def test_portable_zip_contains_app_folder(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_dir = root / "dist" / "WaveScout"
            internal = app_dir / "_internal"
            internal.mkdir(parents=True)
            (app_dir / "WaveScout.exe").write_bytes(b"placeholder")
            (app_dir / "README.txt").write_text("readme", encoding="utf-8")
            (app_dir / "VERSION").write_text("1.0.0", encoding="utf-8")
            (internal / "dependency.dll").write_bytes(b"dll")

            zip_path = root / "dist" / "WaveScout_v1.0.0_Portable.zip"
            build.create_portable_zip(app_dir, zip_path)

            with zipfile.ZipFile(zip_path) as archive:
                names = set(archive.namelist())
            self.assertIn("WaveScout/WaveScout.exe", names)
            self.assertIn("WaveScout/README.txt", names)
            self.assertIn("WaveScout/VERSION", names)
            self.assertIn("WaveScout/_internal/dependency.dll", names)

    def test_netsh_missing_and_command_failures_are_classified(self) -> None:
        class MissingNetshScanner(wavescout.WiFiScanner):
            def _run(self, args):
                return "", -1

        class FailedNetshScanner(wavescout.WiFiScanner):
            def _run(self, args):
                return "The hosted network couldn't be started.", 1

        self.assertEqual(MissingNetshScanner().read_connection()[1], wavescout.ScanError.NETSH)
        self.assertEqual(MissingNetshScanner().scan()[1], wavescout.ScanError.NETSH)
        self.assertEqual(FailedNetshScanner().read_connection()[1], wavescout.ScanError.NETSH)
        self.assertEqual(FailedNetshScanner().scan()[1], wavescout.ScanError.NETSH)

    def test_location_and_no_adapter_failures_are_classified_before_generic_errors(self) -> None:
        class LocationScanner(wavescout.WiFiScanner):
            def _run(self, args):
                return "Location services are turned off.", 1

        class NoAdapterScanner(wavescout.WiFiScanner):
            def _run(self, args):
                return "There is no wireless interface on the system.", 1

        self.assertEqual(LocationScanner().read_connection()[1], wavescout.ScanError.LOCATION)
        self.assertEqual(LocationScanner().scan()[1], wavescout.ScanError.LOCATION)
        self.assertEqual(NoAdapterScanner().read_connection()[1], wavescout.ScanError.NO_ADAPTER)
        self.assertEqual(NoAdapterScanner().scan()[1], wavescout.ScanError.NO_ADAPTER)

    def test_packaged_smoke_exit_is_environment_only(self) -> None:
        source = (ROOT / "wavescout.py").read_text(encoding="utf-8")
        self.assertIn("WAVESCOUT_SMOKE_EXIT_MS", source)
        self.assertIn("QTimer.singleShot", source)

    def test_github_actions_build_verifies_package_without_releasing(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "build-verification.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("python -B build.py", workflow)
        self.assertIn("actions/upload-artifact", workflow)
        self.assertNotIn("softprops/action-gh-release", workflow)
        self.assertNotIn("gh release", workflow.lower())
        self.assertNotIn("git tag", workflow.lower())


if __name__ == "__main__":
    unittest.main()
