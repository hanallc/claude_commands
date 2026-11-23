## Phase style review

Read only the prepared inputs from <StyleReview/>.
Make no edits.

- Range: $range
- Diff with source locations: $diff
- Additions: $additions
- Rust change: $rust

If Rust changed, load the guide with `zsh "$scripts/rust_style/load-rust-style.sh"`.
Use the completed guide load again if you retry in the same conversation.
Confirm the guide summary.
Check each numbered rule against the additions.

Return the report in your review result between the exact markers below.

$begin
| # | Rule | Result |
| --- | --- | --- |
| number | rule name | Pass / VIOLATION: description at path:line / Skip (reason) |
Style review: Pass
$end

For Rust changes, replace the sample row with one row for each numbered rule.
For changes without Rust, replace it with `| — | Rust checklist | Skip (no Rust change) |`.
End the report with exactly one status line.

- Use `Style review: Pass` if the review finds no violations.
- Use `Style review: Findings` if the review finds violations.
- Use `Style review: Error` if a required read or guide load fails.

Include the violation count in your summary for <Synthesize/>.
List each violation as a numbered finding with its severity, title, and path:line.
The runner saves the marked report.
