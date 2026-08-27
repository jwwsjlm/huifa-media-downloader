from __future__ import annotations

import unittest

from scripts.qt_bundle_filter import (
    EXCLUDED_WINDOWS_RUNTIME_MODULES,
    REQUIRED_QT_ARTIFACTS,
    filter_qt_bundle_entries,
    is_unused_qt_artifact,
    validate_required_qt_artifacts,
)


class QtBundleFilterTests(unittest.TestCase):
    def test_unused_qt_module_families_are_pruned(self) -> None:
        unused = (
            r"PySide6\Qt63DRender.dll",
            r"PySide6\Qt6Charts.dll",
            r"PySide6\Qt6DataVisualizationQml.dll",
            r"PySide6\Qt6Graphs.dll",
            r"PySide6\Qt6PdfQuick.dll",
            r"PySide6\Qt6Quick3DRuntimeRender.dll",
            r"PySide6\Qt6SensorsQuick.dll",
            r"PySide6\Qt6SpatialAudio.dll",
            r"PySide6\Qt6TextToSpeech.dll",
            r"PySide6\Qt6VirtualKeyboard.dll",
            r"PySide6\qml\Qt3D\Render\quick3drenderplugin.dll",
            r"PySide6\qml\QtCharts\qtchartsqml2plugin.dll",
            r"PySide6\qml\QtQuick\Pdf\pdfquickplugin.dll",
            r"PySide6\qml\QtQuick\Scene3D\qtquickscene3dplugin.dll",
            r"PySide6\qml\QtQuick3D\qquick3dplugin.dll",
            r"PySide6\plugins\imageformats\qpdf.dll",
            r"PySide6\plugins\qmltooling\qmldbg_quick3dprofiler.dll",
            r"PySide6\plugins\platforminputcontexts\qtvirtualkeyboardplugin.dll",
            r"PySide6\translations\qtbase_de.qm",
        )
        for path in unused:
            with self.subTest(path=path):
                self.assertTrue(is_unused_qt_artifact(path))

    def test_qtwidgets_dependencies_are_preserved(self) -> None:
        required = (
            r"PySide6\Qt6Core.dll",
            r"PySide6\Qt6Gui.dll",
            r"PySide6\Qt6Widgets.dll",
            r"PySide6\plugins\imageformats\qjpeg.dll",
            r"PySide6\plugins\platforms\qwindows.dll",
        )
        for path in required:
            with self.subTest(path=path):
                self.assertFalse(is_unused_qt_artifact(path))

    def test_qml_and_webengine_runtime_are_pruned(self) -> None:
        unused = (
            r"PySide6\Qt6SerialPort.dll",
            r"PySide6\Qt6Qml.dll",
            r"PySide6\Qt6Quick.dll",
            r"PySide6\Qt6WebChannel.dll",
            r"PySide6\Qt6WebEngineCore.dll",
            r"PySide6\Qt6WebEngineWidgets.dll",
            r"PySide6\QtWebEngineProcess.exe",
            r"PySide6\qml\QtQuick\Controls\Basic\Button.qml",
            r"PySide6\qml\QtWebEngine\qtwebenginequickplugin.dll",
            r"PySide6\resources\icudtl.dat",
            r"PySide6\resources\qtwebengine_devtools_resources.pak",
            r"PySide6\resources\v8_context_snapshot.bin",
            r"PySide6\translations\qtwebengine_locales\zh-CN.pak",
        )
        for path in unused:
            with self.subTest(path=path):
                self.assertTrue(is_unused_qt_artifact(path))

    def test_filter_keeps_entry_shape_and_required_artifacts(self) -> None:
        required = [(path, f"source-{index}", "BINARY") for index, path in enumerate(REQUIRED_QT_ARTIFACTS)]
        unused = (r"PySide6\Qt6Pdf.dll", "pdf-source", "BINARY")
        filtered = filter_qt_bundle_entries([*required, unused])

        self.assertEqual(filtered, required)
        validate_required_qt_artifacts(filtered)

    def test_validation_rejects_missing_widget_runtime(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Required Qt runtime artifacts are missing"):
            validate_required_qt_artifacts([])

    def test_release_specs_apply_filter_after_analysis(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        for spec_name in ("HuifaVideoDownloader.lean.spec", "HuifaVideoDownloader.velopack.spec"):
            with self.subTest(spec=spec_name):
                spec = (root / "build" / spec_name).read_text(encoding="utf-8")
                analysis_position = spec.index("a = Analysis(")
                binary_filter_position = spec.index("a.binaries = filter_qt_bundle_entries(a.binaries)")
                data_filter_position = spec.index("a.datas = filter_qt_bundle_entries(a.datas)")
                validation_position = spec.index("validate_required_qt_artifacts(a.binaries, a.datas)")
                self.assertLess(analysis_position, binary_filter_position)
                self.assertLess(binary_filter_position, data_filter_position)
                self.assertLess(data_filter_position, validation_position)

    def test_windows_release_uses_narrow_keyring_hook_and_excludes_build_tooling(self) -> None:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        hook = (root / "scripts" / "pyinstaller_hooks" / "hook-keyring.py").read_text(encoding="utf-8")
        self.assertIn("keyring.backends.Windows", hook)
        self.assertIn("win32ctypes.pywin32.win32cred", hook)
        self.assertNotIn("collect_submodules", hook)
        self.assertNotIn("copy_metadata", hook)
        self.assertIn("setuptools", EXCLUDED_WINDOWS_RUNTIME_MODULES)
        self.assertIn("keyring.backends.macOS", EXCLUDED_WINDOWS_RUNTIME_MODULES)
        for spec_name in ("HuifaVideoDownloader.lean.spec", "HuifaVideoDownloader.velopack.spec"):
            with self.subTest(spec=spec_name):
                spec = (root / "build" / spec_name).read_text(encoding="utf-8")
                self.assertIn("EXCLUDED_WINDOWS_RUNTIME_MODULES", spec)
                self.assertIn("scripts", spec)
                self.assertIn("pyinstaller_hooks", spec)


if __name__ == "__main__":
    unittest.main()
