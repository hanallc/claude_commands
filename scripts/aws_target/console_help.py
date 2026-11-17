"""Share the AWS sign-in instructions and console links."""

import re


PROJECT_NAME = 'natedev builds'
SETTINGS_URL = 'https://settings.aws.com'
CASE_LIST_URL = 'https://support.console.aws.amazon.com/support/home#/case/history'

QUOTA_CASE_STEPS = (
    f'Open {SETTINGS_URL} and sign in with your AWS Builder ID. '
    + 'The 1Password item "aws - browser login for the parent account" is the wrong login for quota cases.',
    'When AWS asks for a passkey, choose "Use passkey" in the 1Password prompt.',
    'A QR code means 1Password did not answer. Unlock 1Password and load the page again.',
    f'Open the console of the project "{PROJECT_NAME}". The top right of the console then shows "{PROJECT_NAME}".',
    f'Open the case list: {CASE_LIST_URL}',
    f'If AWS shows "AWS sign-ins" and asks which sign-in to use, choose "{PROJECT_NAME}".',
)
WRONG_ACCOUNT = 'The hana-team Management Account shows "No cases". That is the wrong account.'
PASSKEY_NOTE = ('If the QR code comes back, open 1Password Settings, then Autofill & save, '
                + 'and turn on "Offer to sign in with passkeys".')


def quota_page_url(region: str, quota_code: str) -> str:
    """Open a regional EC2 quota with a code from quota_targets."""
    if not re.fullmatch(r'[a-z]{2}(?:-[a-z0-9]+)+-[0-9]+', region):
        raise ValueError('Invalid AWS region')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]+', quota_code):
        raise ValueError('Invalid AWS quota code')
    return f'https://{region}.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/{quota_code}'


WAITING_QUOTA_HEADING = 'Waiting on you'
QUOTA_RECORD_TIME = 'Record updated {time}.'
QUOTA_CASE_STATUS = 'See the {case_list} for the case status.'
CASE_LIST_LABEL = 'case list'
QUOTA_REQUEST_COLUMNS = ('Region', 'Quota', 'Asked vCPUs', 'Status', 'Updated')
QUOTA_STATUS_COMMAND = 'deploy.py quota-status'
NO_SAVED_QUOTA_REQUESTS = 'No saved quota requests. Run {command} to read them.'
QUOTA_REQUESTS_READ_FAILURE = 'Could not read the saved quota requests.'
