"""Phone shell: the viewport meta, 16px form controls (no iOS focus zoom) and the keyboard-follow script are served."""
from __future__ import annotations

import re

from axon.features import dashboard_web as w
from axon.features.dash_mobile_css import MOBILE_CSS, MOBILE_JS


def test_viewport_covers_the_notch_and_does_not_lock_zoom():
    assert "viewport-fit=cover" in w.INDEX_HTML and "initial-scale=1" in w.INDEX_HTML
    assert "user-scalable" not in w.INDEX_HTML and "maximum-scale" not in w.INDEX_HTML


def test_form_controls_are_16px_on_touch_and_narrow_screens():
    assert "(max-width:900px),(pointer:coarse)" in MOBILE_CSS
    assert "font-size:16px!important" in MOBILE_CSS and "100dvh" in MOBILE_CSS
    assert "-webkit-text-size-adjust:100%" in MOBILE_CSS and "touch-action:manipulation" in MOBILE_CSS


def test_menus_and_modals_become_bottom_sheets_with_safe_area():
    assert "env(safe-area-inset-bottom)" in MOBILE_CSS and ".wbmenu,.tab-pop,.ctxmenu,.mg-pop" in MOBILE_CSS
    assert not re.search(r"#[0-9a-fA-F]{3,6}\b(?![\w-])", MOBILE_CSS.replace("#app", "").replace("#send-btn", ""))   # theme tokens only, no hard-coded colours


def test_assets_include_the_mobile_layer_last():
    css, _ = w.asset("app.css")
    assert css.rstrip().endswith(MOBILE_CSS.rstrip())
    js, _ = w.asset("app.js")
    assert "visualViewport" in js and MOBILE_JS in js
