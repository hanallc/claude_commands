"""The main column and the title that every console page shares."""

from __future__ import annotations

# Every console page centers its main column in the frame, with the same top
# gutter, so each page title has the same place and style in its column. A page
# sets only its own max-width. A page that measures its layout puts
# layout-measuring on the root element, so it measures with the desktop gutter.
STYLE = ('main{box-sizing:border-box;margin:0 auto;padding:32px 32px 64px}'
         '.page-title{font:700 26px/1.25 system-ui,sans-serif;letter-spacing:normal;margin:0 0 16px}'
         '@media (max-width:560px){:root:not(.layout-measuring) main{padding:24px 16px 48px}}')
