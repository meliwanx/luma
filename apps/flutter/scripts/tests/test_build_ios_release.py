import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


FLUTTER_DIR = Path(__file__).resolve().parents[2]
RELEASE_SCRIPT = FLUTTER_DIR / "scripts" / "build_ios_release.sh"


class BuildIosReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="luma ios release ")
        self.addCleanup(self.temp_dir.cleanup)
        self.work_dir = Path(self.temp_dir.name)
        self.record_path = self.work_dir / "flutter invocation.json"
        recorder = self.work_dir / "record invocation.py"
        recorder.write_text(
            "import json, os, sys\n"
            "with open(os.environ['TEST_RECORD_PATH'], 'w') as output:\n"
            "    json.dump({'cwd': os.getcwd(), 'argv': sys.argv[1:]}, output)\n"
            "sys.exit(int(os.environ.get('TEST_FLUTTER_EXIT_CODE', '0')))\n",
            encoding="utf-8",
        )
        self.fake_flutter = self.work_dir / "flutter"
        self.fake_flutter.write_text(
            '#!/usr/bin/env bash\nexec "$TEST_PYTHON" "$TEST_RECORDER" "$@"\n',
            encoding="utf-8",
        )
        self.fake_flutter.chmod(0o755)
        self.env = os.environ.copy()
        self.env.pop("API_BASE_URL", None)
        self.env.pop("BUILD_NUMBER", None)
        self.env.update(
            PATH=str(self.work_dir) + os.pathsep + self.env.get("PATH", ""),
            TEST_PYTHON=sys.executable,
            TEST_RECORDER=str(recorder),
            TEST_RECORD_PATH=str(self.record_path),
            TEST_FLUTTER_EXIT_CODE="0",
            TEAM_ID="TESTTEAM1",
        )
        self.env.pop("BRAND_DISPLAY_NAME", None)
        self.env.pop("BUNDLE_ID", None)
        self.env["ALLOW_DEFAULT_BRAND"] = "1"

    def run_script(self, script=RELEASE_SCRIPT, **env_overrides):
        env = self.env.copy()
        env.update(env_overrides)
        return subprocess.run(
            [str(script)],
            cwd=str(self.work_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def assert_invocation(self, api_base_url, build_number, flutter_dir=FLUTTER_DIR):
        invocation = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertEqual(Path(invocation["cwd"]).resolve(), flutter_dir.resolve())
        pubspec = (flutter_dir / "pubspec.yaml").read_text(encoding="utf-8")
        version = re.search(r"^version:\s*([^\s+]+)\+\d+\s*$", pubspec, re.MULTILINE)
        self.assertIsNotNone(version)
        build_name = version.group(1)
        self.assertEqual(
            invocation["argv"],
            [
                "build",
                "ipa",
                "--release",
                "--dart-define=API_BASE_URL=" + api_base_url,
                "--dart-define=APP_VERSION=" + build_name + "+" + build_number,
                "--build-number=" + build_number,
                "--export-options-plist=ios/ExportOptions.local.plist",
            ],
        )

    def test_missing_api_base_url_fails_before_flutter(self):
        result = self.run_script()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("API_BASE_URL", result.stderr)
        self.assertFalse(self.record_path.exists())

    def test_empty_api_base_url_fails_before_flutter(self):
        result = self.run_script(API_BASE_URL="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("API_BASE_URL", result.stderr)
        self.assertFalse(self.record_path.exists())

    def test_default_build_number_comes_from_pubspec(self):
        pubspec = (FLUTTER_DIR / "pubspec.yaml").read_text(encoding="utf-8")
        version = re.search(r"^version:\s*\S+\+(\d+)\s*$", pubspec, re.MULTILINE)
        self.assertIsNotNone(version)
        api_base_url = "https://api.invalid"
        result = self.run_script(API_BASE_URL=api_base_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_invocation(api_base_url, version.group(1))

    def test_build_number_override_and_arguments_are_quoted(self):
        api_base_url = "https://api.invalid/path with spaces?a=1&b=$literal;value"
        result = self.run_script(API_BASE_URL=api_base_url, BUILD_NUMBER="42")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_invocation(api_base_url, "42")

    def test_default_build_number_tracks_pubspec_changes(self):
        flutter_dir = self.work_dir / "flutter fixture"
        scripts_dir = flutter_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        release_script = scripts_dir / RELEASE_SCRIPT.name
        shutil.copy2(RELEASE_SCRIPT, release_script)
        (flutter_dir / "pubspec.yaml").write_text(
            "version: 9.8.7+27\n", encoding="utf-8"
        )
        api_base_url = "https://api.invalid"
        result = self.run_script(script=release_script, API_BASE_URL=api_base_url)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_invocation(api_base_url, "27", flutter_dir=flutter_dir)
        result = self.run_script(
            script=release_script, API_BASE_URL=api_base_url, BUILD_NUMBER="42"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assert_invocation(api_base_url, "42", flutter_dir=flutter_dir)

    def test_invalid_build_numbers_fail_before_flutter(self):
        for build_number in ("abc", "-1", "1.2", " 2", "2 3"):
            with self.subTest(build_number=build_number):
                result = self.run_script(
                    API_BASE_URL="https://api.invalid", BUILD_NUMBER=build_number
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("BUILD_NUMBER", result.stderr)
                self.assertFalse(self.record_path.exists())

    def test_missing_team_id_fails_before_flutter(self):
        self.env.pop("TEAM_ID", None)
        result = self.run_script(API_BASE_URL="https://api.invalid")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEAM_ID", result.stderr)
        self.assertFalse(self.record_path.exists())

    def test_empty_team_id_fails_before_flutter(self):
        result = self.run_script(API_BASE_URL="https://api.invalid", TEAM_ID="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEAM_ID", result.stderr)
        self.assertFalse(self.record_path.exists())

    def test_brand_overrides_are_written_to_local_xcconfig(self):
        flutter_dir = self.work_dir / "flutter fixture"
        scripts_dir = flutter_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        release_script = scripts_dir / RELEASE_SCRIPT.name
        shutil.copy2(RELEASE_SCRIPT, release_script)
        (flutter_dir / "pubspec.yaml").write_text(
            "version: 1.2.3+7\n", encoding="utf-8"
        )
        result = self.run_script(
            script=release_script,
            API_BASE_URL="https://api.invalid",
            BUILD_NUMBER="7",
            BUNDLE_ID="app.example.client",
            BRAND_DISPLAY_NAME="Example App",
            TEAM_ID="TEAM123456",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        local = (
            flutter_dir / "ios" / "Flutter" / "Brand.local.xcconfig"
        ).read_text(encoding="utf-8")
        self.assertIn("BRAND_DISPLAY_NAME=Example App\n", local)
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER=app.example.client\n", local)
        self.assertIn("DEVELOPMENT_TEAM=TEAM123456\n", local)
        plist = plistlib.loads(
            (flutter_dir / "ios" / "ExportOptions.local.plist").read_bytes()
        )
        self.assertEqual(plist["teamID"], "TEAM123456")
        invocation = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertIn(
            "--dart-define=BRAND_PRODUCT_NAME=Example App", invocation["argv"]
        )
        self.assertIn(
            "--export-options-plist=ios/ExportOptions.local.plist",
            invocation["argv"],
        )
        committed = (FLUTTER_DIR / "ios" / "Flutter" / "Brand.xcconfig").read_text(
            encoding="utf-8"
        )
        self.assertIn("BRAND_DISPLAY_NAME=Luma", committed)
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER=app.luma.client", committed)
        self.assertIn('#include? "Brand.local.xcconfig"', committed)
        for name in ("Debug.xcconfig", "Release.xcconfig", "Profile.xcconfig"):
            text = (FLUTTER_DIR / "ios" / "Flutter" / name).read_text(
                encoding="utf-8"
            )
            self.assertIn('#include "Brand.xcconfig"', text)

    def test_release_requires_bundle_id_and_display_name(self):
        self.env.pop("ALLOW_DEFAULT_BRAND", None)
        result = self.run_script(
            API_BASE_URL="https://api.invalid",
            TEAM_ID="TEAM123456",
            BRAND_DISPLAY_NAME="聚光测试",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BUNDLE_ID", result.stderr)
        self.assertFalse(self.record_path.exists())
        result = self.run_script(
            API_BASE_URL="https://api.invalid",
            TEAM_ID="TEAM123456",
            BUNDLE_ID="app.juguang.test",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("BRAND_DISPLAY_NAME", result.stderr)
        self.assertFalse(self.record_path.exists())
        result = self.run_script(
            API_BASE_URL="https://api.invalid",
            TEAM_ID="TEAM123456",
            BUNDLE_ID="app.luma.client",
            BRAND_DISPLAY_NAME="Luma",
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("app.luma.client", result.stderr)
        self.assertFalse(self.record_path.exists())

    def test_allow_default_brand_permits_open_source_bundle_id(self):
        result = self.run_script(
            API_BASE_URL="https://api.invalid",
            ALLOW_DEFAULT_BRAND="1",
            BUNDLE_ID="app.luma.client",
            BRAND_DISPLAY_NAME="Luma",
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_juguang_display_name_is_written_to_local_xcconfig(self):
        flutter_dir = self.work_dir / "flutter fixture"
        scripts_dir = flutter_dir / "scripts"
        scripts_dir.mkdir(parents=True)
        release_script = scripts_dir / RELEASE_SCRIPT.name
        shutil.copy2(RELEASE_SCRIPT, release_script)
        (flutter_dir / "pubspec.yaml").write_text(
            "version: 1.2.3+7\n", encoding="utf-8"
        )
        self.env.pop("ALLOW_DEFAULT_BRAND", None)
        result = self.run_script(
            script=release_script,
            API_BASE_URL="https://api.invalid",
            BUILD_NUMBER="7",
            BUNDLE_ID="app.juguang.test",
            BRAND_DISPLAY_NAME="聚光测试",
            TEAM_ID="TEAM123456",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        local = (
            flutter_dir / "ios" / "Flutter" / "Brand.local.xcconfig"
        ).read_text(encoding="utf-8")
        self.assertIn("BRAND_DISPLAY_NAME=聚光测试\n", local)
        self.assertIn("PRODUCT_BUNDLE_IDENTIFIER=app.juguang.test\n", local)
        self.assertIn("DEVELOPMENT_TEAM=TEAM123456\n", local)
        invocation = json.loads(self.record_path.read_text(encoding="utf-8"))
        self.assertIn("--dart-define=BRAND_PRODUCT_NAME=聚光测试", invocation["argv"])

    def test_flutter_failure_exit_code_is_propagated(self):
        api_base_url = "https://api.invalid"
        result = self.run_script(
            API_BASE_URL=api_base_url,
            BUILD_NUMBER="7",
            TEST_FLUTTER_EXIT_CODE="23",
        )
        self.assertEqual(result.returncode, 23)
        self.assert_invocation(api_base_url, "7")


if __name__ == "__main__":
    unittest.main()
