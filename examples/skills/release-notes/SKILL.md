---
name: release-notes
description: Draft user-facing release notes from the git history since the last tag. Use when preparing a release or changelog entry.
allowed-tools: Bash(git describe:*), Bash(git log:*), Bash(git tag:*), Read
argument-hint: "[from-ref] (default: latest tag)"
---
# Release notes

Produce release notes for the changes since `$ARGUMENTS` (or since the latest tag when no ref is given).

1. Find the starting point: `git describe --tags --abbrev=0` (if there are no tags, use the first commit).
2. List commits: `git log --no-merges --pretty=format:"%h %s" <from>..HEAD`.
3. Group them by Conventional Commit type, in this order, skipping empty groups:
   - **Breaking changes** (`!` or `BREAKING CHANGE`)
   - **Features** (`feat`)
   - **Fixes** (`fix`)
   - **Performance** (`perf`)
   - **Docs & chores** (`docs`, `chore`, `refactor`, `build`, `ci`, `test`), collapsed into one line each
4. Rewrite each entry for users: what changed and why it matters, not how. Keep the short hash.
5. If CHANGELOG.md exists, match its heading format and propose the new section at the top; show it to
   me before editing the file.
