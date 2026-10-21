**Correctness.** Does the change do what the spec says, for every acceptance
criterion? Check edge cases (empty, missing, huge, unicode, timezones), error
handling, behaviour changes for existing callers, and whether the change is the
smallest one that works. Flag workarounds that hide a problem instead of fixing it,
such as a `noqa`, a broad `except`, or a hardcoded value.

The other reviewers cover security and test quality.
