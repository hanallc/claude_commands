"""The color tokens and base type of the console pages, one source for each page that uses them."""
from __future__ import annotations

PLACEHOLDER = "/*__CONSOLE_THEME__*/"

_LIGHT = ("--bg:#f6f5f2;--card:#fcfcfb;--line:#dcdad4;--hover:#efede8;--ink:#0b0b0b;--ink-2:#52514e;--ink-3:#66645f;"
          "--accent:#2a78d6;--room:#1baf7a;--error:#c22d2c;--warning:#8a5a00;--edge:rgb(0 0 0 / 0.18)")
_DARK = ("--bg:#121211;--card:#1a1a19;--line:#34332f;--hover:#242422;--ink:#f0efec;--ink-2:#c3c2b7;--ink-3:#9a988f;"
         "--accent:#3987e5;--room:#199e70;--error:#ef7372;--warning:#e3a83f;--edge:rgb(0 0 0 / 0.6)")

CSS = (f":root{{color-scheme:light;{_LIGHT}}}\n"
       f'@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{color-scheme:dark;{_DARK}}}}}\n'
       f':root[data-theme="dark"]{{color-scheme:dark;{_DARK}}}\n'
       "*{box-sizing:border-box}\n"
       'body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}\n'
       "main{max-width:1120px;margin:0 auto;padding:24px 16px 48px}\n"
       "h1{font-size:1.4rem;margin:0;letter-spacing:-0.01em}h2{font-size:1.05rem;margin:0 0 4px}\n"
       ".card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px 18px 14px;margin:0 0 16px}\n"
       ".note{color:var(--ink-2);font-size:0.86rem;margin:6px 0 0;max-width:78ch}\n"
       # A wide table scrolls inside its card, and a shadow marks more columns at the right edge.
       ".scroll{overflow-x:auto;background:linear-gradient(to left,var(--card) 40%,transparent) right/36px 100% no-repeat local,"
       "radial-gradient(farthest-side at 100% 50%,var(--edge),transparent) right/14px 100% no-repeat scroll}\n")


def with_theme(source: str) -> str:
    """Put the theme where the page source holds the placeholder."""
    if PLACEHOLDER not in source:
        raise ValueError(f"the page source has no {PLACEHOLDER}")
    return source.replace(PLACEHOLDER, CSS, 1)
