"""The main column and the title that every console page shares."""

from __future__ import annotations

# Every console page starts its main column at the frame's top-left corner, with
# the same gutter, so each page title has the same place and style. A page sets
# only its own max-width. A page that measures its layout puts layout-measuring on
# the root element, so it measures with the desktop gutter.
STYLE = ('main{box-sizing:border-box;margin:0;padding:32px 32px 64px}'
         '.page-title{font:700 26px/1.25 system-ui,sans-serif;letter-spacing:normal;margin:0 0 16px}'
         '@media (max-width:560px){:root:not(.layout-measuring) main{padding:24px 16px 48px}}')
