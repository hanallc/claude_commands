"""The script version that an open console page compares to reload for a newer script."""

from __future__ import annotations

import hashlib

from scripts.review_links import new_item_dialog, page_frame

# The parts that every page shares: the frame, the owner heading and the New item dialog.
SHARED_PARTS = (page_frame.STYLE, page_frame.REFRESH_SCRIPT, page_frame.PAGE_SCRIPT_ERRORS, page_frame.HEADING_STYLE,
                page_frame.OWNER_STYLE, page_frame.CONSOLE_DIALOG_ASSETS,
                new_item_dialog.STYLE, new_item_dialog.PAGE_STATE_STYLE, new_item_dialog.SCRIPT)


def script_version(*parts: str) -> str:
    """Hash the shared parts and a page's static script, style and markup.

    An open page reloads when its version changes.
    """
    return hashlib.sha256("\0".join((*SHARED_PARTS, *parts)).encode()).hexdigest()[:16]
