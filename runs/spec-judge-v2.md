# Spec adherence judge: v2 calibration (2026-10-20)

Ground truth: the review gate's first verdict per PR (`reference.review` span, earliest per PR).
Judge: `Factory spec adherence` (RXZhbHVhdG9yOjE3NzY1OmlHRzE=), GPT-4.1, temp 0.
v2 = judges/spec_v2.txt (adds a pass over the spec's stated goals/assumptions; conditional criteria permissive).
v1 = judges/spec_v1.txt (acceptance criteria only). v1 labels below are from a fresh re-run today (one-off task, override).

| PR | gate | v1 | v2 | v1 vs gate | v2 vs gate |
|---|---|---|---|---|---|
| #2 | ship | meets_spec | meets_spec | agree | agree |
| #4 | changes | meets_spec | meets_spec | looser | looser |
| #6 | ship | meets_spec | meets_spec | agree | agree |
| #15 | ship | meets_spec | meets_spec | agree | agree |
| #16 | ship | meets_spec | meets_spec | agree | agree |
| #17 | ship | misses_spec | meets_spec | stricter | agree |
| #18 | changes | meets_spec | meets_spec | looser | looser |
| #19 | ship | meets_spec | meets_spec | agree | agree |
| #20 | ship | meets_spec | meets_spec | agree | agree |
| #21 | ship | meets_spec | meets_spec | agree | agree |
| #22 | ship | meets_spec | meets_spec | agree | agree |
| #31 | ship | meets_spec | misses_spec | agree | stricter |
| #32 | ship | meets_spec | meets_spec | agree | agree |
| #33 | ship | meets_spec | meets_spec | agree | agree |
| #34 | changes | meets_spec | meets_spec | looser | looser |
| #35 | ship | meets_spec | meets_spec | agree | agree |
| #36 | changes | misses_spec | meets_spec | agree | looser |
| #37 | ship | meets_spec | meets_spec | agree | agree |
| #38 | changes | meets_spec | meets_spec | looser | looser |
| #47 | ship | meets_spec | meets_spec | agree | agree |
| #48 | changes | meets_spec | meets_spec | looser | looser |
| #49 | changes | meets_spec | meets_spec | looser | looser |
| #50 | ship | meets_spec | not scored | agree | n/a |
| #51 | ship | meets_spec | meets_spec | agree | agree |
| #52 | ship | meets_spec | meets_spec | agree | agree |
| #53 | ship | meets_spec | meets_spec | agree | agree |
| #54 | ship | meets_spec | not scored | agree | n/a |

Calibration set (19 PRs, #2-#38): v1 {'agree': 14, 'looser': 4, 'stricter': 1}, v2 {'agree': 13, 'looser': 5, 'stricter': 1}.
Batch 3 (#47-#54): v1 {'agree': 6, 'looser': 2}, v2 {'agree': 4, 'looser': 2, 'n/a': 2}.
Earlier v1 run (Milestone 5) on the 19: 13 agree / 1 stricter (#17) / 5 looser. Today's v1 re-run also fails #36, so v1 varies run to run on #36 even at temp 0.

## Findings

- v2 fixed the #17 strict fail (conditional criterion now permissive) but added a new false fail on #31: it failed criteria asking the builder to *run* commands locally, which no diff can evidence.
- v2 still passes #34, the one gate verdict that is genuinely a spec gap (reviews paginated but never reach the crew). Its explanation asserts that tests show later-page reviewers reaching the crew, but the gate found `find_crew` never reads `data.reviews`; the crew test it cites covers a commit author on page 2, not a reviewer. The goal-tracing pass produced a confident, wrong justification, the same failure mode as review_correctness v2.
- v2 loses #36 (stale example PNGs), which v1 caught on today's run.
- Most 'looser' rows are not spec failures at all: #18 (SSRF), #4 (tests skip in CI), #38 (token echo), #49 (gate reviewed against a stale base). A spec judge should pass those; the miss judges own them. The only fair target is #34.

## Decision

Not promoted. v2 is not better than v1 (calibration: 13 agree vs 14 for v1) and still misses #34. Evaluator version 3 republishes the v1 template, so the continuous task `Factory judges (reference review)` (unpinned, uses latest version) is back on v1. v2 had been live from 18:47 UTC, so batch 3 spans were first scored with v2; a one-off task (T25saW5lVGFzazo0NDI1NDpYYmRM, override) re-scored all 27 reference spans with v1 so live labels are consistent. The miss judges were not touched.

Post beat: a judge change gets the same gate as a prompt change. The 'smarter' v2 judge read better and scored worse. Limitation to state: the spec judge only sees the diff, so it can't catch a gap like #34 where the missing piece is code that was never written; that is the correctness reviewer's job (and v3 of that reviewer now catches it).
