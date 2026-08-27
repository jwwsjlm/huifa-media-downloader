from __future__ import annotations

"""Pure policy for choosing the bundled or standalone yt-dlp core."""

from dataclasses import dataclass


CORE_MODE_AUTO = "auto"
CORE_MODE_EXTERNAL = "external"
CORE_MODE_BUILTIN = "builtin"


def normalize_ytdlp_core_mode(value: object) -> str:
    mode = str(value or "").strip().casefold()
    return mode if mode in {
        CORE_MODE_AUTO,
        CORE_MODE_EXTERNAL,
        CORE_MODE_BUILTIN,
    } else CORE_MODE_AUTO


@dataclass(frozen=True, slots=True)
class YtdlpCoreSelection:
    mode: str
    backend: str
    executable: str = ""
    external_version: str | None = None
    external_rejected: bool = False

    @property
    def uses_external(self) -> bool:
        return self.backend == CORE_MODE_EXTERNAL


class YtdlpCoreSelectionError(RuntimeError):
    def __init__(self, message: str, *, mode: str, reason: str) -> None:
        super().__init__(message)
        self.mode = normalize_ytdlp_core_mode(mode)
        self.reason = str(reason or "unavailable")


def select_ytdlp_core(
    mode: object,
    *,
    external_executable: object = "",
    external_version: str | None = None,
    builtin_available: bool,
    packaged: bool = False,
) -> YtdlpCoreSelection:
    """Choose one usable core from already-discovered runtime evidence.

    ``external_version is None`` means startup diagnostics have not checked
    the executable yet, so it remains eligible for an optimistic launch.
    An empty string means diagnostics checked it and found it unusable.
    """

    normalized_mode = normalize_ytdlp_core_mode(mode)
    executable = str(external_executable or "").strip()
    external_rejected = bool(executable and external_version == "")
    external_available = bool(executable and not external_rejected)

    if normalized_mode == CORE_MODE_EXTERNAL:
        if external_available:
            return YtdlpCoreSelection(
                mode=normalized_mode,
                backend=CORE_MODE_EXTERNAL,
                executable=executable,
                external_version=external_version,
            )
        raise YtdlpCoreSelectionError(
            "已选择外置 yt-dlp 核心，但没有找到可运行的 yt-dlp.exe；请在“检查运行组件”中下载安装或更新",
            mode=normalized_mode,
            reason=("external_probe_failed" if external_rejected else "external_missing"),
        )

    if normalized_mode == CORE_MODE_BUILTIN:
        if builtin_available:
            return YtdlpCoreSelection(
                mode=normalized_mode,
                backend=CORE_MODE_BUILTIN,
            )
        raise YtdlpCoreSelectionError(
            "已选择内置 yt-dlp 核心，但当前主程序没有可用的内置模块；请更新或重新下载完整主程序",
            mode=normalized_mode,
            reason="builtin_missing",
        )

    if external_available:
        return YtdlpCoreSelection(
            mode=normalized_mode,
            backend=CORE_MODE_EXTERNAL,
            executable=executable,
            external_version=external_version,
        )
    if builtin_available:
        return YtdlpCoreSelection(
            mode=normalized_mode,
            backend=CORE_MODE_BUILTIN,
            external_version=external_version,
            external_rejected=external_rejected,
        )
    message = (
        "外置 yt-dlp.exe 不可用，且内置 yt-dlp 下载核心加载失败"
        if packaged
        else "未找到可用的外置 yt-dlp，当前开发环境也未安装 yt-dlp"
    )
    raise YtdlpCoreSelectionError(
        message,
        mode=normalized_mode,
        reason=("all_unavailable_after_probe" if external_rejected else "all_missing"),
    )
