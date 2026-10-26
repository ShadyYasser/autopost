import copy
import shutil
from pathlib import Path

import pytest
import yaml

from autopost.config import ConfigError, load_config, placeholder_values

ROOT = Path(__file__).resolve().parents[1]


def make_config(tmp_path: Path, mutate=None) -> Path:
    shutil.copytree(ROOT / "config", tmp_path / "config")
    if mutate:
        path = tmp_path / "config" / "brands" / "takwin.yaml"
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        mutate(data)
        path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return tmp_path / "config"


def test_shipped_config_loads(app):
    assert set(app.brands) == {"takwin", "kalima_sawaa"}
    k = app.brands["kalima_sawaa"]
    assert k.streams_for_slot("night") == ["short", "long"]
    long_yt = k.platform_for("long", "youtube")
    assert long_yt.provider == "direct" and long_yt.shorts_check is None
    assert k.platform_for("long", "facebook").format == "video"
    assert k.platform_for("short", "facebook").format == "reel"
    assert app.brands["takwin"].platforms["instagram"].ig_user_id == "17841455147710531"
    assert app.settings.meta.graph_version == "v26.0"


def test_placeholders_reported(app):
    assert "brands.takwin.text_source.sheet_id" in placeholder_values(app)


def test_required_secrets_follow_config(app):
    secrets = app.required_secrets()
    assert "META_PAGE_TOKEN_TAKWIN" in secrets and "BUFFER_YOUTUBE_API_KEY" in secrets
    # youtube_direct is disabled → no YouTube OAuth secrets needed yet
    assert "YOUTUBE_CLIENT_ID" not in secrets


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d["streams"]["main"]["slots"].append("noon"), "unknown slot 'noon'"),
    (lambda d: d["templates"]["facebook"].update(text="{caption} {hashtags}"), "['hashtags']"),
    (lambda d: d["platforms"]["tiktok"].update(provider="direct"), "provider 'direct' not supported"),
    (lambda d: d["platforms"]["tiktok"].update(buffer_account="nope"), "unknown buffer_account"),
    (lambda d: d["slots"]["fajr"].update(offset_minutes=-3), "must be before the prayer"),
    (lambda d: d["templates"].pop("pinterest"), "no entry under `templates`"),
    (lambda d: d["slots"]["third"]["windows"].pop(), "at least 2"),
    (lambda d: d["slots"].update(bad={"type": "fixed", "at": "25:00"}), "HH:MM"),
])
def test_invalid_brand_config(tmp_path, mutate, message):
    with pytest.raises(ConfigError) as exc:
        load_config(make_config(tmp_path, mutate))
    assert message in str(exc.value)


def test_new_brand_is_a_file(tmp_path):
    cfg = make_config(tmp_path)
    data = yaml.safe_load((cfg / "brands" / "kalima_sawaa.yaml").read_text(encoding="utf-8"))
    new = copy.deepcopy(data)
    new["id"], new["name"] = "new_brand", "جديد"
    (cfg / "brands" / "new_brand.yaml").write_text(yaml.safe_dump(new, allow_unicode=True), encoding="utf-8")
    assert "new_brand" in load_config(cfg).brands
