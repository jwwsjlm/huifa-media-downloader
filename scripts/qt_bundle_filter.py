from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import TypeVar


# PyInstaller's PySide6.QtQml hook collects every QML module shipped in the
# wheel. Analysis.excludes prevents the Python wrappers below from being
# imported, but it does not stop their QML plugins and native dependencies
# from being added to the bundle. Keep this list aligned with the native/QML
# pruning rules so the intent is explicit in the spec file.
EXCLUDED_PYSIDE_MODULES = (
    "PySide6.Qt3DAnimation",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DExtras",
    "PySide6.Qt3DInput",
    "PySide6.Qt3DLogic",
    "PySide6.Qt3DRender",
    "PySide6.QtCharts",
    "PySide6.QtDataVisualization",
    "PySide6.QtGraphs",
    "PySide6.QtGraphsWidgets",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtPrintSupport",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.QtQuick3D",
    "PySide6.QtQuickControls2",
    "PySide6.QtQuickWidgets",
    "PySide6.QtSensors",
    "PySide6.QtSerialBus",
    "PySide6.QtSerialPort",
    "PySide6.QtSpatialAudio",
    "PySide6.QtTextToSpeech",
    "PySide6.QtWebChannel",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
)


# These Python modules are either build tooling or non-Windows keyring
# backends. They are excluded from both the portable and installer builds;
# SecureStore explicitly uses keyring.backends.Windows.WinVaultKeyring.
EXCLUDED_WINDOWS_RUNTIME_MODULES = (
    "_distutils_hack",
    "setuptools",
    "keyring.backends.SecretService",
    "keyring.backends.chainer",
    "keyring.backends.kwallet",
    "keyring.backends.libsecret",
    "keyring.backends.macOS",
    "keyring.backends.null",
    "keyring.cli",
    "keyring.devpi_client",
    "dbus",
    "gi",
    "jeepney",
    "secretstorage",
)


_UNUSED_QML_PREFIXES = (
    "pyside6/qml/",
)

_UNUSED_NATIVE_PREFIXES = (
    "qt63d",
    "qt6charts",
    "qt6datavisualization",
    "qt6graphs",
    "qt6pdf",
    "qt6positioning",
    "qt6printsupport",
    "qt6qml",
    "qt6quick",
    "qt6quick3d",
    "qt6sensors",
    "qt6serialbus",
    "qt6serialport",
    "qt6spatialaudio",
    "qt6texttospeech",
    "qt6virtualkeyboard",
    "qt6webchannel",
    "qt6webengine",
)

_UNUSED_EXACT_PATHS = frozenset(
    {
        "pyside6/qtwebengineprocess.exe",
        "pyside6/plugins/generic/qtuiotouchplugin.dll",
        "pyside6/plugins/imageformats/qpdf.dll",
        "pyside6/plugins/qmltooling/qmldbg_quick3dprofiler.dll",
        "pyside6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
        "pyside6/resources/qtwebengine_devtools_resources.debug.pak",
        "pyside6/resources/qtwebengine_resources.debug.pak",
        "pyside6/resources/qtwebengine_resources_100p.debug.pak",
        "pyside6/resources/qtwebengine_resources_200p.debug.pak",
        "pyside6/resources/v8_context_snapshot.debug.bin",
    }
)


_UNUSED_PATH_PREFIXES = (
    # The application supplies its own Chinese UI strings and does not install
    # a QTranslator. Bundled Qt .qm catalogs were therefore extracted on every
    # launch but never loaded.
    "pyside6/translations/",
    "pyside6/translations/qtwebengine_locales/",
    "pyside6/resources/qtwebengine",
    "pyside6/resources/v8_context_snapshot",
    "pyside6/resources/icudtl.dat",
)

_PRESERVED_PATH_PREFIXES: tuple[str, ...] = ()


# Fail the release build if a future filter edit or PyInstaller/PySide layout
# change silently drops a GUI-critical artifact. Transitive dependencies are
# still left to PyInstaller's dependency analysis.
REQUIRED_QT_ARTIFACTS = frozenset(
    {
        "pyside6/qt6core.dll",
        "pyside6/qt6gui.dll",
        "pyside6/qt6network.dll",
        "pyside6/qt6widgets.dll",
        "pyside6/plugins/imageformats/qjpeg.dll",
        "pyside6/plugins/platforms/qwindows.dll",
    }
)


Entry = TypeVar("Entry", bound=Sequence[object])


def normalize_bundle_path(path: object) -> str:
    """Return a stable, case-insensitive PyInstaller destination path."""

    return str(path).replace("\\", "/").lstrip("./").casefold()


def is_unused_qt_artifact(destination: object) -> bool:
    """Whether a bundle destination belongs to an explicitly unused Qt module."""

    path = normalize_bundle_path(destination)
    filename = path.rsplit("/", 1)[-1]
    if path in _UNUSED_EXACT_PATHS:
        return True
    if any(path.startswith(prefix) for prefix in _PRESERVED_PATH_PREFIXES):
        return False
    if any(path.startswith(prefix) for prefix in _UNUSED_PATH_PREFIXES):
        return True
    if any(path.startswith(prefix) for prefix in _UNUSED_QML_PREFIXES):
        return True
    if not path.startswith("pyside6/"):
        return False
    return any(filename.startswith(prefix) for prefix in _UNUSED_NATIVE_PREFIXES)


def filter_qt_bundle_entries(entries: Iterable[Entry]) -> list[Entry]:
    """Remove only the known-unused Qt artifacts from a PyInstaller TOC."""

    return [entry for entry in entries if entry and not is_unused_qt_artifact(entry[0])]


def validate_required_qt_artifacts(*entry_groups: Iterable[Sequence[object]]) -> None:
    """Abort a release build that is missing a QWidget-critical Qt artifact."""

    destinations = {
        normalize_bundle_path(entry[0])
        for entries in entry_groups
        for entry in entries
        if entry
    }
    missing = sorted(REQUIRED_QT_ARTIFACTS - destinations)
    if missing:
        formatted = ", ".join(missing)
        raise RuntimeError(f"Required Qt runtime artifacts are missing: {formatted}")
