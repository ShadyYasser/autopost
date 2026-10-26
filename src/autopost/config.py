"""Configuration models, loading and validation.

settings.yaml holds global options; config/brands/*.yaml holds one brand per file.
Adding a brand means adding a YAML file (plus any GitHub Secrets it names).
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Annotated, Any, Literal, Union
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PLATFORMS = ("facebook", "instagram", "tiktok", "youtube", "pinterest")
PlatformName = Literal["facebook", "instagram", "tiktok", "youtube", "pinterest"]
PrayerName = Literal["fajr", "sunrise", "dhuhr", "asr", "maghrib", "isha"]

# Which providers each platform supports.
ALLOWED_PROVIDERS: dict[str, set[str]] = {
    "facebook": {"direct", "buffer"},
    "instagram": {"direct", "buffer"},
    "tiktok": {"buffer"},
    "youtube": {"buffer", "direct"},
    "pinterest": {"direct", "buffer"},
}

# Template fields each platform needs.
TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "facebook": ("text",),
    "instagram": ("text",),
    "tiktok": ("text",),
    "youtube": ("title", "description"),
    "pinterest": ("title", "description"),
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class ConfigError(Exception):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _check_hhmm(value: str) -> str:
    if not _HHMM.match(value):
        raise ValueError(f"time must be HH:MM (24h), got {value!r}")
    return value


def _check_secret_name(value: str | None) -> str | None:
    if value is not None and not _SECRET_NAME.match(value):
        raise ValueError(f"secret names must be UPPER_SNAKE_CASE, got {value!r}")
    return value


def _check_range(value: tuple[int, int]) -> tuple[int, int]:
    lo, hi = value
    if lo < 0 or hi < lo:
        raise ValueError(f"range must be [min, max] with 0 <= min <= max, got {list(value)}")
    return value


# ---------------------------------------------------------------- settings


class PrayerTimesConfig(_Model):
    method: str = "EGYPTIAN"
    latitude: float
    longitude: float
    adjustments_minutes: dict[PrayerName, int] = Field(default_factory=dict)

    @field_validator("method")
    @classmethod
    def _known_method(cls, v: str) -> str:
        from adhanpy.calculation.CalculationMethod import CalculationMethod

        if v not in CalculationMethod.__members__:
            raise ValueError(f"unknown method {v!r}; one of {sorted(CalculationMethod.__members__)}")
        return v


class SchedulerConfig(_Model):
    lookahead_minutes: int = Field(40, ge=0, le=300)
    platform_gap_seconds: tuple[int, int] = (120, 600)
    max_lateness_minutes: int = Field(180, ge=1)
    deadline_margin_seconds: int = Field(180, ge=0)
    min_slot_spacing_minutes: int = Field(60, ge=0)

    v_range = field_validator("platform_gap_seconds")(_check_range)


class GoogleConfig(_Model):
    service_account_secret: str
    v_s = field_validator("service_account_secret")(_check_secret_name)


class TelegramConfig(_Model):
    bot_token_secret: str
    chat_id_secret: str
    v_s = field_validator("bot_token_secret", "chat_id_secret")(_check_secret_name)


class MediaHostingConfig(_Model):
    github_release_tag: str = "media-temp"


class BufferLimits(_Model):
    per_15min: int = 100
    per_day: int = 100
    per_30days: int = 3000
    max_queued: int = 10
    safety_margin: int = 10


class BufferPoll(_Model):
    interval_seconds: int = Field(30, ge=5)
    timeout_minutes: int = Field(20, ge=1)


class BufferAccountConfig(_Model):
    api_key_secret: str
    limits: BufferLimits = Field(default_factory=BufferLimits)
    poll: BufferPoll = Field(default_factory=BufferPoll)
    v_s = field_validator("api_key_secret")(_check_secret_name)


class MetaConfig(_Model):
    graph_version: str = "v26.0"


class PinterestConfig(_Model):
    environment: Literal["sandbox", "production"] = "sandbox"
    app_id_secret: str
    app_secret_secret: str
    v_s = field_validator("app_id_secret", "app_secret_secret")(_check_secret_name)


class YoutubeDirectConfig(_Model):
    enabled: bool = False
    client_id_secret: str
    client_secret_secret: str
    v_s = field_validator("client_id_secret", "client_secret_secret")(_check_secret_name)


class Settings(_Model):
    timezone: str
    cron_enabled: bool = False
    prayer_times: PrayerTimesConfig | None = None
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    max_attempts_per_delivery: int = Field(3, ge=1)
    low_content_warning_days: int = Field(3, ge=0)
    # Override built-in text limits, e.g. {instagram: {text: 2000}}
    text_limits: dict[str, dict[str, int]] = Field(default_factory=dict)
    daily_summary_time: str = "23:30"
    google: GoogleConfig
    telegram: TelegramConfig
    media_hosting: MediaHostingConfig = Field(default_factory=MediaHostingConfig)
    buffer_accounts: dict[str, BufferAccountConfig] = Field(default_factory=dict)
    meta: MetaConfig = Field(default_factory=MetaConfig)
    pinterest: PinterestConfig
    youtube_direct: YoutubeDirectConfig

    v_t = field_validator("daily_summary_time")(_check_hhmm)

    @field_validator("timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except Exception as exc:  # noqa: BLE001
            raise ValueError(f"unknown timezone {v!r}") from exc
        return v

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


# ---------------------------------------------------------------- slots


class Anchor(_Model):
    """A point in time on a slot's day: a fixed clock time or a prayer time, plus offsets."""

    time: str | None = None
    prayer: PrayerName | None = None
    offset_minutes: int = 0
    day_offset: int = Field(0, ge=0, le=1)

    @model_validator(mode="after")
    def _one_base(self) -> "Anchor":
        if (self.time is None) == (self.prayer is None):
            raise ValueError("an anchor needs exactly one of `time` or `prayer`")
        if self.time is not None:
            _check_hhmm(self.time)
        return self


class Window(_Model):
    label: str | None = None
    start: Anchor
    end: Anchor
    weight: float = Field(1.0, gt=0)


class _SlotBase(_Model):
    # Override of scheduler.platform_gap_seconds for posts of this slot.
    gap_seconds: tuple[int, int] | None = None
    # Optional generic hard deadline: every platform must be done before it.
    deadline: Anchor | None = None
    # Override of scheduler.max_lateness_minutes.
    max_lateness_minutes: int | None = Field(None, ge=1)

    @field_validator("gap_seconds")
    @classmethod
    def _gap(cls, v: tuple[int, int] | None) -> tuple[int, int] | None:
        return None if v is None else _check_range(v)

    def uses_prayer(self) -> bool:
        return self.deadline is not None and self.deadline.prayer is not None


class FixedSlot(_SlotBase):
    type: Literal["fixed"]
    at: str
    jitter_minutes: int = Field(0, ge=0, le=720)

    v_t = field_validator("at")(_check_hhmm)


class PrayerSlot(_SlotBase):
    type: Literal["prayer_relative"]
    prayer: PrayerName
    offset_minutes: int = 0
    jitter_minutes: int = Field(0, ge=0, le=720)
    # The prayer time itself is a hard deadline for the whole post.
    hard_deadline: bool = True

    @model_validator(mode="after")
    def _before_prayer(self) -> "PrayerSlot":
        if self.hard_deadline and self.offset_minutes + self.jitter_minutes >= 0:
            raise ValueError(
                "with hard_deadline the latest possible target (offset + jitter) must be before the prayer"
            )
        if self.hard_deadline and self.deadline is not None:
            raise ValueError("use either hard_deadline or deadline, not both")
        return self

    def uses_prayer(self) -> bool:
        return True


class RandomWindowSlot(_SlotBase):
    type: Literal["random_window"]
    window: Window

    def uses_prayer(self) -> bool:
        return super().uses_prayer() or any(a.prayer for a in (self.window.start, self.window.end))


class PickWindowSlot(_SlotBase):
    type: Literal["pick_window"]
    windows: list[Window] = Field(min_length=2)

    def uses_prayer(self) -> bool:
        return super().uses_prayer() or any(a.prayer for w in self.windows for a in (w.start, w.end))


SlotConfig = Annotated[
    Union[FixedSlot, PrayerSlot, RandomWindowSlot, PickWindowSlot], Field(discriminator="type")
]


# ---------------------------------------------------------------- text sources


class SheetTextSource(_Model):
    type: Literal["sheet"]
    sheet_id: str
    worksheet_index: int = Field(0, ge=0)   # 0 = first tab (by position, not by name)
    header_rows: int = Field(1, ge=0)
    columns: dict[str, str]
    posted_at_column: str = "C"

    @field_validator("columns")
    @classmethod
    def _cols(cls, v: dict[str, str]) -> dict[str, str]:
        for name, col in v.items():
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
                raise ValueError(f"column variable name {name!r} is not a valid identifier")
            _check_column(col, name)
        return v

    @field_validator("posted_at_column")
    @classmethod
    def _posted(cls, v: str) -> str:
        return _check_column(v, "posted_at_column")

    def variables(self) -> set[str]:
        return set(self.columns)


def _check_column(col: str, name: str) -> str:
    if not re.match(r"^[A-Z]{1,2}$", col):
        raise ValueError(f"column for {name!r} must be a letter like A, got {col!r}")
    return col


class FilenameTextSource(_Model):
    type: Literal["filename"]
    # Removed from the start of the title (e.g. Drive's "Copy of " when a file was copied).
    strip_prefixes: list[str] = Field(default_factory=list)
    # Character replacements applied to the title (e.g. full-width "：" back to ":").
    replacements: dict[str, str] = Field(default_factory=dict)

    def variables(self) -> set[str]:
        return {"title"}


TextSource = Annotated[Union[SheetTextSource, FilenameTextSource], Field(discriminator="type")]


# ---------------------------------------------------------------- platforms


class ShortsCheck(_Model):
    vertical: bool = True
    max_seconds: int = Field(180, ge=1)


class PlatformConfig(_Model):
    """Routing + platform options. Which fields are required depends on platform and provider."""

    provider: Literal["direct", "buffer"]
    # buffer
    buffer_account: str | None = None
    channel: str | None = None
    # facebook / instagram
    page_id: str | None = None
    ig_user_id: str | None = None
    token_secret: str | None = None
    format: Literal["reel", "video"] = "reel"
    # youtube
    shorts_check: ShortsCheck | None = None
    fallback: Literal["direct"] | None = None
    privacy: Literal["private", "unlisted", "public"] = "private"
    category_id: str = "27"
    made_for_kids: bool = False
    refresh_token_secret: str | None = None
    # pinterest
    board_id: str | None = None

    v_s = field_validator("token_secret", "refresh_token_secret")(_check_secret_name)


class TemplateConfig(_Model):
    text: str | None = None
    title: str | None = None
    description: str | None = None


class StreamConfig(_Model):
    drive_folder_id: str
    slots: list[str] = Field(min_length=1)
    platforms: list[PlatformName] = Field(min_length=1)
    overrides: dict[PlatformName, dict[str, Any]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _folder(self) -> "StreamConfig":
        if len(set(self.platforms)) != len(self.platforms):
            raise ValueError("duplicate platform in stream")
        return self


class BrandConfig(_Model):
    id: str
    name: str
    enabled: bool = True
    slots: dict[str, SlotConfig] = Field(min_length=1)
    prayer_times: PrayerTimesConfig | None = None
    min_slot_spacing_minutes: int | None = Field(None, ge=0)
    text_source: TextSource
    streams: dict[str, StreamConfig] = Field(min_length=1)
    templates: dict[PlatformName, TemplateConfig]
    platforms: dict[PlatformName, PlatformConfig]

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not re.match(r"^[a-z][a-z0-9_]*$", v):
            raise ValueError("brand id must be lower_snake_case")
        return v

    def platform_for(self, stream: str, platform: str) -> PlatformConfig:
        """Effective platform config for a stream (brand config + stream overrides)."""
        base = self.platforms[platform].model_dump()
        base.update(self.streams[stream].overrides.get(platform, {}))
        return PlatformConfig.model_validate(base)

    def streams_for_slot(self, slot: str) -> list[str]:
        return [name for name, s in self.streams.items() if slot in s.slots]


class AppConfig(BaseModel):
    settings: Settings
    brands: dict[str, BrandConfig]

    def enabled_brands(self) -> list[BrandConfig]:
        return [b for b in self.brands.values() if b.enabled]

    def prayer_config(self, brand: BrandConfig) -> PrayerTimesConfig | None:
        return brand.prayer_times or self.settings.prayer_times

    def required_secrets(self) -> dict[str, list[str]]:
        """Secret name -> list of places that need it (only for enabled brands)."""
        need: dict[str, list[str]] = {}

        def add(name: str | None, where: str) -> None:
            if name:
                need.setdefault(name, []).append(where)

        s = self.settings
        add(s.google.service_account_secret, "google")
        add(s.telegram.bot_token_secret, "telegram")
        add(s.telegram.chat_id_secret, "telegram")
        for brand in self.enabled_brands():
            for stream_name, stream in brand.streams.items():
                for p in stream.platforms:
                    cfg = brand.platform_for(stream_name, p)
                    where = f"{brand.id}/{p}"
                    if cfg.provider == "buffer":
                        add(s.buffer_accounts[cfg.buffer_account].api_key_secret, where)
                    if p in ("facebook", "instagram") and cfg.provider == "direct":
                        add(cfg.token_secret, where)
                    if p == "pinterest" and cfg.provider == "direct":
                        add(s.pinterest.app_id_secret, where)
                        add(s.pinterest.app_secret_secret, where)
                        add(cfg.refresh_token_secret, where)
                    if p == "youtube" and s.youtube_direct.enabled and (
                        cfg.provider == "direct" or cfg.fallback == "direct"
                    ):
                        add(s.youtube_direct.client_id_secret, where)
                        add(s.youtube_direct.client_secret_secret, where)
                        add(cfg.refresh_token_secret, where)
        return need


# ---------------------------------------------------------------- cross-field validation


def _template_vars(template: str) -> set[str]:
    names = set()
    for _, field_name, _, _ in string.Formatter().parse(template):
        if field_name is not None:
            if field_name == "" or not field_name.isidentifier():
                raise ValueError(f"invalid placeholder {{{field_name}}} in template {template!r}")
            names.add(field_name)
    return names


def validate_brand(brand: BrandConfig, settings: Settings) -> list[str]:
    errors: list[str] = []
    p = f"brand {brand.id}"

    needs_prayer = any(s.uses_prayer() for s in brand.slots.values())
    if needs_prayer and not (brand.prayer_times or settings.prayer_times):
        errors.append(f"{p}: prayer-based slots need `prayer_times` in settings or in the brand")

    variables = brand.text_source.variables()
    for name, stream in brand.streams.items():
        sp = f"{p} stream {name}"
        for slot in stream.slots:
            if slot not in brand.slots:
                errors.append(f"{sp}: unknown slot {slot!r} (defined: {sorted(brand.slots)})")
        for platform in stream.overrides:
            if platform not in stream.platforms:
                errors.append(f"{sp}: override for {platform} which the stream does not publish to")
        for platform in stream.platforms:
            pp = f"{sp} {platform}"
            if platform not in brand.platforms:
                errors.append(f"{pp}: no entry under `platforms`")
                continue
            try:
                cfg = brand.platform_for(name, platform)
            except ValidationError as exc:
                errors.append(f"{pp}: invalid override: {exc.errors()[0]['msg']}")
                continue
            errors.extend(_validate_platform(pp, platform, cfg, settings))
            tmpl = brand.templates.get(platform)
            if tmpl is None:
                errors.append(f"{pp}: no entry under `templates`")
                continue
            for fld in TEMPLATE_FIELDS[platform]:
                value = getattr(tmpl, fld)
                if not value:
                    errors.append(f"{pp}: template needs `{fld}`")
                    continue
                try:
                    unknown = _template_vars(value) - variables
                except ValueError as exc:
                    errors.append(f"{pp}: {exc}")
                    continue
                if unknown:
                    errors.append(
                        f"{pp}: template {fld} uses {sorted(unknown)}; available: {sorted(variables)}"
                    )
    return errors


def _validate_platform(pp: str, platform: str, cfg: PlatformConfig, settings: Settings) -> list[str]:
    errors = []
    if cfg.provider not in ALLOWED_PROVIDERS[platform]:
        errors.append(f"{pp}: provider {cfg.provider!r} not supported (use {sorted(ALLOWED_PROVIDERS[platform])})")
    if cfg.provider == "buffer":
        if not cfg.buffer_account or not cfg.channel:
            errors.append(f"{pp}: buffer provider needs `buffer_account` and `channel`")
        elif cfg.buffer_account not in settings.buffer_accounts:
            errors.append(f"{pp}: unknown buffer_account {cfg.buffer_account!r}")
        if platform == "pinterest" and not cfg.board_id:
            errors.append(f"{pp}: pinterest via buffer needs `board_id`")
    elif platform == "facebook" and not (cfg.page_id and cfg.token_secret):
        errors.append(f"{pp}: needs `page_id` and `token_secret`")
    elif platform == "instagram" and not (cfg.ig_user_id and cfg.token_secret):
        errors.append(f"{pp}: needs `ig_user_id` and `token_secret`")
    elif platform == "pinterest" and not (cfg.board_id and cfg.refresh_token_secret):
        errors.append(f"{pp}: needs `board_id` and `refresh_token_secret`")
    if platform == "youtube":
        uses_direct = cfg.provider == "direct" or cfg.fallback == "direct"
        if uses_direct and settings.youtube_direct.enabled and not cfg.refresh_token_secret:
            errors.append(f"{pp}: direct YouTube upload needs `refresh_token_secret`")
        if cfg.fallback and cfg.provider == "direct":
            errors.append(f"{pp}: `fallback` only makes sense with provider buffer")
    return errors


# ---------------------------------------------------------------- loading


def _load_yaml(path: Path) -> Any:
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc


def _format_validation(path: Path, exc: ValidationError) -> str:
    lines = [f"{path}:"]
    for err in exc.errors():
        loc = ".".join(str(x) for x in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_dir: str | Path = "config") -> AppConfig:
    config_dir = Path(config_dir)
    settings_path = config_dir / "settings.yaml"
    if not settings_path.exists():
        raise ConfigError(f"missing {settings_path}")
    try:
        settings = Settings.model_validate(_load_yaml(settings_path))
    except ValidationError as exc:
        raise ConfigError(_format_validation(settings_path, exc)) from exc

    brands: dict[str, BrandConfig] = {}
    problems: list[str] = []
    brand_files = sorted((config_dir / "brands").glob("*.y*ml"))
    if not brand_files:
        raise ConfigError(f"no brand files in {config_dir / 'brands'}")
    for path in brand_files:
        try:
            brand = BrandConfig.model_validate(_load_yaml(path))
        except ValidationError as exc:
            problems.append(_format_validation(path, exc))
            continue
        if brand.id in brands:
            problems.append(f"{path}: duplicate brand id {brand.id!r}")
            continue
        problems.extend(f"{path}: {e}" for e in validate_brand(brand, settings))
        brands[brand.id] = brand
    if problems:
        raise ConfigError("\n".join(problems))
    return AppConfig(settings=settings, brands=brands)


def placeholder_values(app: AppConfig) -> list[str]:
    """Config values still set to REPLACE_WITH_... (reported by `validate`)."""
    found: list[str] = []

    def walk(obj: Any, path: str) -> None:
        if isinstance(obj, BaseModel):
            for k in type(obj).model_fields:
                walk(getattr(obj, k), f"{path}.{k}" if path else k)
        elif isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{path}.{k}")
        elif isinstance(obj, (list, tuple)):
            for i, v in enumerate(obj):
                walk(v, f"{path}[{i}]")
        elif isinstance(obj, str) and obj.startswith("REPLACE_WITH"):
            found.append(path)

    for brand in app.brands.values():
        walk(brand, f"brands.{brand.id}")
    return found
