---
description: Review a pull request or branch diff against the base branch
argument-hint: "[base-branch]  (default: main)"
allowed-tools: Bash(git diff:*), Bash(git log:*), Bash(git merge-base:*)
---
Review the changes on the current branch against the base branch "$ARGUMENTS"
(if that is empty, use `main`, or `master` when `main` does not exist).

Steps:
1. Run `git merge-base HEAD <base>` and remember the commit it prints as BASE.
2. Run `git log --oneline BASE..HEAD` to list the commits in this branch.
3. Run `git diff BASE...HEAD` and read the changed files where more context is needed.
4. Write the review as:
   - **Summary**: what the change does, in 2-3 sentences.
   - **Blocking issues**: bugs, security problems, broken behaviour (each with `path:line`).
   - **Suggestions**: tests to add, simplifications, naming.
   - **Verdict**: approve or request changes.
