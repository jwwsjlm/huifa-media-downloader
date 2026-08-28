from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from conf import BASE_DIR
from uploader.baijiahao_uploader.main import (
    BaiJiaHaoVideo,
    baijiahao_setup,
    cookie_auth as baijiahao_cookie_auth,
)
from uploader.alipay_uploader.main import (
    AlipayVideo,
    alipay_setup,
    cookie_auth as alipay_cookie_auth,
)
from uploader.bilibili_uploader.browser_login import login_bilibili_with_playwright
from uploader.bilibili_uploader.runtime import run_biliup_command
from uploader.douyin_uploader.main import (
    DOUYIN_PUBLISH_STRATEGY_IMMEDIATE,
    DOUYIN_PUBLISH_STRATEGY_SCHEDULED,
    DouYinVideo,
    cookie_auth as douyin_cookie_auth,
    douyin_setup,
)
from uploader.ks_uploader.main import (
    KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE,
    KUAISHOU_PUBLISH_STRATEGY_SCHEDULED,
    KSVideo,
    cookie_auth as kuaishou_cookie_auth,
    ks_setup,
)
from uploader.tencent_uploader.main import (
    TENCENT_PUBLISH_STRATEGY_IMMEDIATE,
    TENCENT_PUBLISH_STRATEGY_SCHEDULED,
    TencentVideo,
    cookie_auth as tencent_cookie_auth,
    tencent_setup,
)
from uploader.weibo_uploader.main import (
    WeiBoVideo,
    weibo_setup,
    cookie_auth as weibo_cookie_auth,
)
from uploader.hupu_uploader.main import (
    HuPuVideo,
    hupu_setup,
    cookie_auth as hupu_cookie_auth,
)
from uploader.xiaohongshu_uploader.main import (
    XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE,
    XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED,
    XiaoHongShuVideo,
    cookie_auth as xiaohongshu_cookie_auth,
    xiaohongshu_setup,
)
from uploader.youtube_uploader.main import (
    YouTubeVideo,
    cookie_auth as youtube_cookie_auth,
    youtube_cookie_gen,
    youtube_setup,
)

SCHEDULE_FORMAT = "%Y-%m-%d %H:%M"


@dataclass(slots=True)
class DouyinVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    thumbnail_landscape_file: Path | None = None
    thumbnail_portrait_file: Path | None = None
    product_link: str = ""
    product_title: str = ""
    publish_strategy: str = DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    declaration: str | None = None
    collection_name: str | None = None


@dataclass(slots=True)
class KuaishouVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    publish_strategy: str = KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    collection_name: str | None = None


@dataclass(slots=True)
class XiaohongshuVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    publish_strategy: str = XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class BilibiliVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tid: int
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None


@dataclass(slots=True)
class TencentVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    publish_date: datetime | int
    thumbnail_file: Path | None = None
    thumbnail_landscape_file: Path | None = None
    thumbnail_portrait_file: Path | None = None
    short_title: str | None = None
    category: str | None = None
    is_draft: bool = False
    publish_strategy: str = TENCENT_PUBLISH_STRATEGY_IMMEDIATE
    debug: bool = True
    headless: bool = True
    collection_name: str | None = None


@dataclass(slots=True)
class BaijiahaoVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    thumbnail_file: Path | None = None
    collection_name: str | None = None
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class AlipayVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    thumbnail_file: Path | None = None
    collection_name: str | None = None
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class WeiboVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    thumbnail_file: Path | None = None
    collection_name: str | None = None
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class HupuVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    thumbnail_file: Path | None = None
    debug: bool = True
    headless: bool = True


@dataclass(slots=True)
class YouTubeVideoUploadRequest:
    account_name: str
    video_file: Path
    title: str
    description: str
    tags: list[str]
    thumbnail_file: Path | None = None
    playlist: str | None = None
    visibility: str = "public"
    debug: bool = True
    headless: bool = False


def resolve_runtime_home() -> Path:
    return Path(BASE_DIR)


def resolve_account_file(platform: str, account_name: str) -> Path:
    account_file = resolve_runtime_home() / "cookies" / f"{platform}_{account_name}.json"
    account_file.parent.mkdir(exist_ok=True)
    return account_file


def parse_tags(raw_tags: str | None) -> list[str]:
    if not raw_tags:
        return []

    tags: list[str] = []
    for item in raw_tags.split(","):
        cleaned = item.strip().lstrip("#")
        if cleaned:
            tags.append(cleaned)
    return tags


def parse_schedule(raw_schedule: str | None) -> datetime | int:
    if not raw_schedule:
        return 0
    return datetime.strptime(raw_schedule, SCHEDULE_FORMAT)


async def login_douyin_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("douyin", account_name)
    return await douyin_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_douyin_account(account_name: str) -> bool:
    account_file = resolve_account_file("douyin", account_name)
    if not account_file.exists():
        return False
    return await douyin_cookie_auth(str(account_file))


async def login_kuaishou_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("kuaishou", account_name)
    return await ks_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_kuaishou_account(account_name: str) -> bool:
    account_file = resolve_account_file("kuaishou", account_name)
    if not account_file.exists():
        return False
    return await kuaishou_cookie_auth(str(account_file))


async def login_xiaohongshu_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("xiaohongshu", account_name)
    return await xiaohongshu_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_xiaohongshu_account(account_name: str) -> bool:
    account_file = resolve_account_file("xiaohongshu", account_name)
    if not account_file.exists():
        return False
    return await xiaohongshu_cookie_auth(str(account_file))


async def login_bilibili_account(account_name: str, headless: bool = False) -> dict:
    account_file = resolve_account_file("bilibili", account_name)
    return await login_bilibili_with_playwright(
        account_name,
        account_file,
        headless=headless,
    )


async def check_bilibili_account(account_name: str) -> bool:
    account_file = resolve_account_file("bilibili", account_name)
    if not account_file.exists():
        return False
    result = run_biliup_command(["-u", str(account_file), "renew"])
    return result.returncode == 0


async def login_tencent_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("tencent", account_name)
    return await tencent_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_tencent_account(account_name: str) -> bool:
    account_file = resolve_account_file("tencent", account_name)
    if not account_file.exists():
        return False
    return await tencent_cookie_auth(str(account_file))


async def login_youtube_account(account_name: str, headless: bool = False) -> dict:
    account_file = resolve_account_file("youtube", account_name)
    # A user clicking "login" expects a visible browser immediately.  Do not
    # run youtube_setup's hidden cookie preflight first: on a slow or blocked
    # connection that invisible navigation can consume the full timeout and
    # make the desktop application look frozen before any window appears.
    return await youtube_cookie_gen(str(account_file), headless=headless)


async def check_youtube_account(account_name: str) -> bool:
    account_file = resolve_account_file("youtube", account_name)
    if not account_file.exists():
        return False
    return await youtube_cookie_auth(str(account_file))


async def upload_youtube_video(request: YouTubeVideoUploadRequest) -> Path:
    account_file = resolve_account_file("youtube", request.account_name)
    is_ready = await youtube_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"YouTube cookie is missing or expired: {account_file}. Run `sau youtube login --account {request.account_name}` first."
        )

    app = YouTubeVideo(
        request.title,
        str(request.video_file),
        request.tags,
        str(account_file),
        description=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        playlist=request.playlist,
        visibility=request.visibility,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def upload_video(request: DouyinVideoUploadRequest) -> Path:
    account_file = resolve_account_file("douyin", request.account_name)
    is_ready = await douyin_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Douyin cookie is missing or expired: {account_file}. Run `sau douyin login --account {request.account_name}` first."
        )

    app = DouYinVideo(
        request.title,
        str(request.video_file),
        request.tags,
        request.publish_date,
        str(account_file),
        desc=request.description,
        thumbnail_landscape_path=(
            str(request.thumbnail_landscape_file) if request.thumbnail_landscape_file else None
        ),
        thumbnail_portrait_path=str(
            request.thumbnail_portrait_file or request.thumbnail_file
        ) if request.thumbnail_portrait_file or request.thumbnail_file else None,
        productLink=request.product_link,
        productTitle=request.product_title,
        declaration=request.declaration,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        collection_name=request.collection_name,
    )
    await app.douyin_upload_video()
    return account_file


async def upload_kuaishou_video(request: KuaishouVideoUploadRequest) -> Path:
    account_file = resolve_account_file("kuaishou", request.account_name)
    is_ready = await ks_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Kuaishou cookie is missing or expired: {account_file}. Run `sau kuaishou login --account {request.account_name}` first."
        )

    app = KSVideo(
        title=request.title,
        file_path=str(request.video_file),
        desc=request.description,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        collection_name=request.collection_name,
    )
    await app.main()
    return account_file


async def upload_xiaohongshu_video(request: XiaohongshuVideoUploadRequest) -> Path:
    account_file = resolve_account_file("xiaohongshu", request.account_name)
    is_ready = await xiaohongshu_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Xiaohongshu cookie is missing or expired: {account_file}. Run `sau xiaohongshu login --account {request.account_name}` first."
        )

    app = XiaoHongShuVideo(
        title=request.title,
        file_path=str(request.video_file),
        desc=request.description,
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def upload_bilibili_video(request: BilibiliVideoUploadRequest) -> Path:
    account_file = resolve_account_file("bilibili", request.account_name)
    if not account_file.exists():
        raise RuntimeError(
            f"Bilibili account file is missing: {account_file}. Run `sau bilibili login --account {request.account_name}` first."
        )

    arguments = [
        "-u",
        str(account_file),
        "upload",
        str(request.video_file),
        "--title",
        request.title,
        "--desc",
        request.description,
        "--tid",
        str(request.tid),
    ]
    if request.tags:
        arguments.extend(["--tag", ",".join(request.tags)])
    if request.thumbnail_file:
        arguments.extend(["--cover", str(request.thumbnail_file)])
    if isinstance(request.publish_date, datetime):
        arguments.extend(["--dtime", str(int(request.publish_date.timestamp()))])

    result = run_biliup_command(arguments)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "").strip() or "Bilibili upload failed")
    return account_file


async def upload_tencent_video(request: TencentVideoUploadRequest) -> Path:
    account_file = resolve_account_file("tencent", request.account_name)
    is_ready = await tencent_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Tencent/WeChat Channels cookie is missing or expired: {account_file}. "
            f"Run `sau tencent login --account {request.account_name}` first."
        )

    app = TencentVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        publish_date=request.publish_date,
        account_file=str(account_file),
        category=request.category,
        is_draft=request.is_draft,
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        thumbnail_landscape_path=(
            str(request.thumbnail_landscape_file) if request.thumbnail_landscape_file else None
        ),
        thumbnail_portrait_path=(
            str(request.thumbnail_portrait_file) if request.thumbnail_portrait_file else None
        ),
        short_title=request.short_title,
        publish_strategy=request.publish_strategy,
        debug=request.debug,
        headless=request.headless,
        collection_name=request.collection_name,
    )
    await app.tencent_upload_video()
    return account_file


async def login_baijiahao_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("baijiahao", account_name)
    return await baijiahao_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_baijiahao_account(account_name: str) -> bool:
    account_file = resolve_account_file("baijiahao", account_name)
    if not account_file.exists():
        return False
    return await baijiahao_cookie_auth(str(account_file))


async def upload_baijiahao_video(request: BaijiahaoVideoUploadRequest) -> Path:
    account_file = resolve_account_file("baijiahao", request.account_name)
    is_ready = await baijiahao_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Baijiahao cookie is missing or expired: {account_file}. Run `sau baijiahao login --account {request.account_name}` first."
        )

    app = BaiJiaHaoVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        account_file=str(account_file),
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        collection_name=request.collection_name,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def login_alipay_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("alipay", account_name)
    return await alipay_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_alipay_account(account_name: str) -> bool:
    account_file = resolve_account_file("alipay", account_name)
    if not account_file.exists():
        return False
    return await alipay_cookie_auth(str(account_file))


async def upload_alipay_video(request: AlipayVideoUploadRequest) -> Path:
    account_file = resolve_account_file("alipay", request.account_name)
    is_ready = await alipay_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Alipay cookie is missing or expired: {account_file}. Run `sau alipay login --account {request.account_name}` first."
        )

    app = AlipayVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        account_file=str(account_file),
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        collection_name=request.collection_name,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def login_weibo_account(account_name: str, headless: bool = True) -> dict:
    account_file = resolve_account_file("weibo", account_name)
    return await weibo_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_weibo_account(account_name: str) -> bool:
    account_file = resolve_account_file("weibo", account_name)
    if not account_file.exists():
        return False
    return await weibo_cookie_auth(str(account_file))


async def upload_weibo_video(request: WeiboVideoUploadRequest) -> Path:
    account_file = resolve_account_file("weibo", request.account_name)
    is_ready = await weibo_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Weibo cookie is missing or expired: {account_file}. Run `sau weibo login --account {request.account_name}` first."
        )

    app = WeiBoVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        account_file=str(account_file),
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        collection_name=request.collection_name,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


async def login_hupu_account(account_name: str, headless: bool = False) -> dict:
    account_file = resolve_account_file("hupu", account_name)
    return await hupu_setup(str(account_file), handle=True, return_detail=True, headless=headless)


async def check_hupu_account(account_name: str) -> bool:
    account_file = resolve_account_file("hupu", account_name)
    if not account_file.exists():
        return False
    return await hupu_cookie_auth(str(account_file))


async def upload_hupu_video(request: HupuVideoUploadRequest) -> Path:
    account_file = resolve_account_file("hupu", request.account_name)
    is_ready = await hupu_setup(str(account_file), handle=False)
    if not is_ready:
        raise RuntimeError(
            f"Hupu cookie is missing or expired: {account_file}. Run `sau hupu login --account {request.account_name}` first."
        )

    app = HuPuVideo(
        title=request.title,
        file_path=str(request.video_file),
        tags=request.tags,
        account_file=str(account_file),
        desc=request.description,
        thumbnail_path=str(request.thumbnail_file) if request.thumbnail_file else None,
        debug=request.debug,
        headless=request.headless,
    )
    await app.main()
    return account_file


def _payload_text(payload: dict, key: str, *, required: bool = False) -> str:
    value = str(payload.get(key) or "").strip()
    if required and not value:
        raise ValueError(f"Missing required upload field: {key}")
    return value


def _payload_file(payload: dict, key: str, *, required: bool = False) -> Path | None:
    value = _payload_text(payload, key, required=required)
    if not value:
        return None
    path = Path(value)
    if not path.is_file():
        raise ValueError(f"File not found: {value}")
    return path


def _payload_tags(payload: dict) -> list[str]:
    value = payload.get("tags")
    if isinstance(value, (list, tuple, set)):
        return [
            tag
            for tag in (str(item).strip().lstrip("#") for item in value)
            if tag
        ]
    return parse_tags(str(value or ""))


async def publish_video_payload(platform: str, payload: dict) -> str:
    """Publish one video from the desktop app's structured in-process request."""

    platform_key = str(platform or "").strip().casefold()
    if not isinstance(payload, dict):
        raise TypeError("Upload payload must be a mapping")

    account = _payload_text(payload, "account_name", required=True)
    video = _payload_file(payload, "video_file", required=True)
    title = _payload_text(payload, "title", required=True)
    description = _payload_text(payload, "description")
    tags = _payload_tags(payload)
    schedule_text = _payload_text(payload, "schedule")
    publish_date = parse_schedule(schedule_text)
    thumbnail = _payload_file(payload, "thumbnail_file")
    landscape = _payload_file(payload, "thumbnail_landscape_file")
    portrait = _payload_file(payload, "thumbnail_portrait_file")
    collection = _payload_text(payload, "collection_name") or None
    debug = bool(payload.get("debug", False))
    headless = bool(payload.get("headless", True))

    if platform_key == "douyin":
        strategy = (
            DOUYIN_PUBLISH_STRATEGY_SCHEDULED
            if publish_date
            else DOUYIN_PUBLISH_STRATEGY_IMMEDIATE
        )
        await upload_video(DouyinVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            publish_date=publish_date,
            thumbnail_file=thumbnail,
            thumbnail_landscape_file=landscape,
            thumbnail_portrait_file=portrait,
            product_link=_payload_text(payload, "product_link"),
            product_title=_payload_text(payload, "product_title"),
            declaration=_payload_text(payload, "declaration") or None,
            collection_name=collection,
            publish_strategy=strategy,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "kuaishou":
        strategy = (
            KUAISHOU_PUBLISH_STRATEGY_SCHEDULED
            if publish_date
            else KUAISHOU_PUBLISH_STRATEGY_IMMEDIATE
        )
        await upload_kuaishou_video(KuaishouVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            publish_date=publish_date,
            thumbnail_file=thumbnail,
            collection_name=collection,
            publish_strategy=strategy,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "xiaohongshu":
        if len(tags) > 10:
            raise ValueError(f"Xiaohongshu accepts at most 10 tags; received {len(tags)}")
        strategy = (
            XIAOHONGSHU_PUBLISH_STRATEGY_SCHEDULED
            if publish_date
            else XIAOHONGSHU_PUBLISH_STRATEGY_IMMEDIATE
        )
        await upload_xiaohongshu_video(XiaohongshuVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            publish_date=publish_date,
            thumbnail_file=thumbnail,
            publish_strategy=strategy,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "bilibili":
        try:
            tid = int(payload.get("tid") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("Bilibili tid must be a positive integer") from exc
        if tid <= 0:
            raise ValueError("Bilibili tid must be a positive integer")
        await upload_bilibili_video(BilibiliVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tid=tid,
            tags=tags,
            publish_date=publish_date,
            thumbnail_file=thumbnail,
        ))
    elif platform_key == "tencent":
        strategy = (
            TENCENT_PUBLISH_STRATEGY_SCHEDULED
            if publish_date
            else TENCENT_PUBLISH_STRATEGY_IMMEDIATE
        )
        await upload_tencent_video(TencentVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            publish_date=publish_date,
            thumbnail_file=thumbnail,
            thumbnail_landscape_file=landscape,
            thumbnail_portrait_file=portrait,
            short_title=_payload_text(payload, "short_title") or None,
            category=_payload_text(payload, "category") or None,
            is_draft=bool(payload.get("is_draft", False)),
            collection_name=collection,
            publish_strategy=strategy,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "baijiahao":
        await upload_baijiahao_video(BaijiahaoVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            thumbnail_file=thumbnail,
            collection_name=collection,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "alipay":
        await upload_alipay_video(AlipayVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            thumbnail_file=thumbnail,
            collection_name=collection,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "weibo":
        await upload_weibo_video(WeiboVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            thumbnail_file=thumbnail,
            collection_name=collection,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "hupu":
        await upload_hupu_video(HupuVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            thumbnail_file=thumbnail,
            debug=debug,
            headless=headless,
        ))
    elif platform_key == "youtube":
        visibility = _payload_text(payload, "visibility") or "public"
        if visibility not in {"public", "unlisted", "private"}:
            raise ValueError("YouTube visibility must be public, unlisted, or private")
        await upload_youtube_video(YouTubeVideoUploadRequest(
            account_name=account,
            video_file=video,
            title=title,
            description=description,
            tags=tags,
            thumbnail_file=thumbnail,
            playlist=_payload_text(payload, "playlist") or None,
            visibility=visibility,
            debug=debug,
            headless=headless,
        ))
    else:
        raise ValueError(f"Unsupported platform: {platform_key or platform}")

    return f"{platform_key} 发布流程已完成"
