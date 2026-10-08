"""macOS and Windows display names come from brand configuration."""

import unittest
from pathlib import Path


FLUTTER = Path(__file__).resolve().parents[2]


class DesktopBrandNameTests(unittest.TestCase):
    def test_macos_product_name_follows_brand_display_name(self):
        text = (FLUTTER / "macos/Runner/Configs/AppInfo.xcconfig").read_text(
            encoding="utf-8"
        )
        self.assertIn('#include? "Brand.local.xcconfig"', text)
        self.assertIn("BRAND_DISPLAY_NAME = $(BRAND_DISPLAY_NAME:default=Luma)", text)
        self.assertIn("PRODUCT_NAME = $(BRAND_DISPLAY_NAME)", text)
        self.assertNotIn("luma_client", text)

    def test_windows_window_title_and_product_name_are_injected(self):
        main = (FLUTTER / "windows/runner/main.cpp").read_text(encoding="utf-8")
        rc = (FLUTTER / "windows/runner/Runner.rc").read_text(encoding="utf-8")
        header = (FLUTTER / "windows/runner/brand_config.h").read_text(encoding="utf-8")
        cmake = (FLUTTER / "windows/CMakeLists.txt").read_text(encoding="utf-8")
        self.assertIn("brandWindowTitle()", main)
        self.assertNotIn('L"luma_client"', main)
        self.assertNotIn('VALUE "ProductName", "luma_client"', rc)
        self.assertIn('VALUE "ProductName", BRAND_DISPLAY_NAME_UTF8', rc)
        self.assertIn('#define BRAND_DISPLAY_NAME_UTF8 "Luma"', header)
        self.assertIn("BRAND_DISPLAY_NAME", cmake)
        self.assertIn('set(BRAND_DISPLAY_NAME "Luma")', cmake)

    def test_android_release_requires_brand_app_id(self):
        text = (FLUTTER / "android/app/build.gradle.kts").read_text(encoding="utf-8")
        self.assertIn("brandAppId", text)
        self.assertIn("allowDefaultBrand", text)
        self.assertIn("ALLOW_DEFAULT_BRAND", text)
        self.assertIn('name.contains("Release")', text)
        self.assertIn("app.luma.client", text)


if __name__ == "__main__":
    unittest.main()
