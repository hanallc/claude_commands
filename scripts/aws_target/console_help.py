"""Share the AWS sign-in instructions and console links."""

import re


CASE_LIST_URL = 'https://support.console.aws.amazon.com/support/home#/case/history'

QUOTA_CASE_STEPS = (
    'Open the 1Password item "aws - natedev - console-admin account" and open its website.',
    'Sign in with the user name and password from that item. This login uses no passkey and no QR code.',
    f'Open the case list: {CASE_LIST_URL}',
)
WRONG_ACCOUNT = 'The other AWS items in 1Password sign in to the hana-team parent account. It shows "No cases".'


def quota_page_url(region: str, quota_code: str) -> str:
    """Open a regional EC2 quota with a code from quota_targets."""
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+', region):
        raise ValueError('Invalid AWS region')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]+', quota_code):
        raise ValueError('Invalid AWS quota code')
    return f'https://{region}.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/{quota_code}'
