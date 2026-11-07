## Phase style review

Edit nothing. Skip every write step; read only these prepared inputs instead
of running the input preparation in <StyleReview/>:
- Range: $range
- Diff (with source locations): $diff
- Additions (reading aid): $additions
- Rust change: $rust

If Rust changed, load the guide once for this phase, reusing a completed load
in this thread on a retry: `zsh "$scripts/rust_style/load-rust-style.sh"`.
Confirm its summary and walk every numbered checklist rule against the
additions. Otherwise skip the Rust checklist with reason `no Rust change`.
Run `"$scripts/lib/py" "$scripts/hooks/banned_words_lib.py" --diff < "$diff"`.
This mode scans additions without updating counters. Exit 0 is Pass, 1 is a
violation; any other exit is Error. Do not recreate the scanner or rule list.

Return the report inside these exact markers in your normal review result:
$begin
| # | Rule | Result |
| --- | --- | --- |
| number | rule name | Pass / VIOLATION: description at path:line / Skip (reason) |
Style review: Pass
$end

Replace the sample row with every applicable checklist row and the scanner
result. End with exactly one `Style review: Pass` or `Style review: Findings`;
use `Style review: Error` if a required read, guide load or scan fails. Include
the violation count. Include every violation as a numbered finding with
severity, title and file:line in your normal summary for <Synthesize/>. The
runner saves the marked report; do not write the report or findings ledger.
