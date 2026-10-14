---
description: Fix a GitHub issue (number or URL) end to end, with a test
argument-hint: "<issue number or URL>"
allowed-tools: Bash(gh issue view:*), Bash(git status:*), Bash(git diff:*)
---
Fix issue $1.

1. Read the issue: run `gh issue view $1` (if the GitHub CLI is not installed, ask me to paste the issue text).
2. Reproduce the problem: find the code involved and, when possible, write a failing test first.
3. Implement the smallest fix that makes the test pass without breaking other behaviour.
4. Run the relevant tests and show the result.
5. Summarise the root cause, the fix and the files changed. Suggest a commit message
   like `fix: <summary> (#$1)`, but do not commit unless I ask.

Extra context from me (may be empty): $ARGUMENTS
