"""Locate the published AWS Machines console page."""

from pathlib import Path

from scripts.aws_target import aws_page
from scripts.review_links import review_link

PAGE_NAME = "machines.html"


def output_folder() -> Path:
    return aws_page.output_folder()


def page_url() -> str:
    """Return the URL for the published AWS Machines page."""
    page = output_folder().resolve() / PAGE_NAME
    if not page.is_file():
        raise FileNotFoundError(f"The AWS Machines page is not available: {page}")
    return review_link.snapshot_url(review_link.review_root().resolve(), page)
