"""Check the shared AWS console instructions and URL builder."""

from scripts.aws_target import console_help, deploy, ledger_test_support


class ConsoleHelpTests(ledger_test_support.CostFixture):
    def test_quota_case_instructions_name_the_sign_in_and_case_list(self) -> None:
        self.assertEqual(console_help.QUOTA_CASE_STEPS, (
            'Open https://settings.aws.com and sign in with your AWS Builder ID.',
            'Open the console of the project "natedev builds". The top right of the console then shows "natedev builds".',
            'Open the case list: https://support.console.aws.amazon.com/support/home#/case/history',
            'If AWS shows "AWS sign-ins" and asks which sign-in to use, choose "natedev builds".',
        ))
        self.assertEqual(console_help.WRONG_ACCOUNT,
                         'The hana-team Management Account shows "No cases". That is the wrong account.')

    def test_quota_page_urls_use_the_configured_regions_and_quota_codes(self) -> None:
        for regions, code, _desired, _label in deploy.quota_targets('us-east-2'):
            for region in regions:
                self.assertEqual(console_help.quota_page_url(region, code),
                                 f'https://{region}.console.aws.amazon.com/servicequotas/home/services/ec2/quotas/{code}')

    def test_invalid_region_or_code_cannot_change_the_url(self) -> None:
        for region, code in (('us-west-2.example/', 'L-fixture'), ('us-west-2', 'L-fixture?account=other')):
            with self.subTest(region=region, code=code), self.assertRaises(ValueError):
                _ = console_help.quota_page_url(region, code)
