**Correctness.** Does the change do what the spec says, for every acceptance
criterion? Check edge cases (empty, missing, huge, unicode, timezones), error
handling, behaviour changes for existing callers, and whether the change is the
smallest one that works. Flag workarounds that hide a problem instead of fixing it,
such as a `noqa`, a broad `except`, or a hardcoded value.

Don't stop at "the new code looks right". Before you approve, do these two checks.
They catch the bugs that pass every test.

1. **Follow the data to where it's used.** For every value the change adds, fetches
   or accepts (a new field, a new API call, a new parameter, more pages of results),
   find the code that consumes it and confirm it changes the output the user sees.
   Read the callers and the consumers, not only the changed lines. Fetching or
   storing data that nothing downstream reads is a major finding when the spec or
   issue expects that data to show up.

2. **Check every combination of inputs to each changed function.** When a function
   gains a parameter, check each way it can be called: the parameter alone, with
   the other optional arguments, and with none. An explicit argument that is
   silently ignored on one path (for example, when a client, cache or default is
   also supplied) is a major finding.

Judge against what the issue asks for and the assumptions the spec states, not
only the literal wording of each acceptance criterion. If the feature would not
work for a user in the way the issue describes, that is at least major.

The other reviewers cover security and test quality.
