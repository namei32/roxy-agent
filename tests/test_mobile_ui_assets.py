from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "src" / "akasha" / "mobile_ui.js").read_text(encoding="utf-8")
STYLES = (ROOT / "src" / "akasha" / "mobile_ui.css").read_text(encoding="utf-8")
PLUGIN = (ROOT / "src" / "akasha" / "plugin.py").read_text(encoding="utf-8")


def test_recall_lanes_have_distinct_semantic_classes() -> None:
    assert 'left, "precise"' in SCRIPT
    assert 'right, "completion"' in SCRIPT
    assert "akasha-mobile-recall--${escapeHtml(lane)}" in SCRIPT


def test_recall_lanes_use_material_tonal_surfaces() -> None:
    assert "--akasha-mobile-precise: var(--roxy-color-action-primary)" in STYLES
    assert "--akasha-mobile-completion:" in STYLES
    assert "var(--roxy-color-status-trace)" in STYLES
    assert "grid-template-columns: 4px minmax(0, 1fr) auto" in STYLES
    assert "transition-property: all" not in STYLES
    assert "transition: all" not in STYLES


def test_mobile_recall_preserves_semantic_lanes_with_lazy_paint() -> None:
    assert "for raw in value:" in PLUGIN
    assert "value[:" not in PLUGIN
    assert "content-visibility: auto" in STYLES
    assert "contain-intrinsic-block-size: auto 94px" in STYLES
