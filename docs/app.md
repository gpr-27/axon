# The Axon app (browser)

> **Status: under active development.** The app is not finished. Pages, layout and features change between
> commits, and some parts may be incomplete or rough. The terminal is the supported way to use Axon; treat
> the app as an extra view onto the same data.

The app is a local web page served by your own Axon process. It shows and drives the same sessions, files,
settings and usage data that the terminal uses.

## Starting it

```bash
axon open                       # start a server, print the link, open your browser
axon open --no-open             # print the link only
axon open --port 9000           # default 8787; a free port is used if it is busy; 0 picks a random one
axon open --workspace DIR       # the project to show (default: the current directory)
axon dash                       # same thing under its older name
```

Inside a session, `/open` does the same (aliases `/dash`, `/web`); `/open stop` stops it. A page opened from
`/open` is attached to that running session, so replies you send from the page are queued onto it rather than
written by a second process. `axon open` on its own starts a separate server that is not tied to a terminal
session; `Ctrl+C` stops it. The browser is not opened over SSH, in CI, on a Linux machine with no display, or
when `AXON_NO_BROWSER` is set; the link is printed instead.

## What it contains

The sections, as named in the page: Chat, Files, Shell, Terminal, Commands, Insights, Sessions, Usage, Memory,
Extensions and Settings. Chat sends messages to a session and shows its transcript. Files browses and edits files
in the project. Shell runs single commands line by line (no full-screen programs). Commands, Insights, Usage,
Sessions, Memory, Extensions and Settings give browser views of the matching slash commands, usage data, saved
sessions, memory files, installed extensions and settings. Secret-looking settings are masked.

## Security model

The server is meant for you alone on your own machine.

- **Loopback only.** It binds to `127.0.0.1` and there is no option to listen on any other address. Asking for
  another host raises an error.
- **Private link.** The link carries a random token (`?t=...`). The server swaps it for an `HttpOnly`,
  `SameSite=Strict` cookie and redirects, so the token does not stay in the address bar. Every request needs the
  cookie. Anyone who has the link before it is used, or can read your browser's cookies, has the access
  described below, so do not share it.
- **Request checks.** Requests must carry a loopback `Host` header (a defence against DNS rebinding). Every POST
  must also come from the same origin and carry a JSON body (a defence against cross-site requests).
- **No outside loads.** The page, its script, styles, icon and colours are built into Axon and served by the
  server itself. The content security policy allows only same-origin resources, so the app works without a
  network connection and loads nothing from a CDN or font host.
- **Untrusted text.** Transcript text is escaped before any HTML is built, and secrets are redacted from
  what the server sends. Session ids are looked up in an index, never joined into a path.
- **Replies do not auto-approve.** A reply sent from the page runs in `default`, `plan` or `acceptEdits` mode
  (bypass is never offered), and each tool approval is shown in the page. Approvals time out after 5 minutes.
- **Sharing.** The only action that sends data off the machine is the optional secret gist export, which
  uploads the previewed file after a confirmation click.

### The Terminal tab is a full shell on your computer

The Terminal tab starts a real shell (your `$SHELL`) or the Axon program in a pseudo-terminal, in the project
folder, and sends it every key you type. It is the same as a terminal window: **it can run any command as you.**
It does not go through Axon's permission rules, the shell guard or the Bash sandbox. The page says so on the tab.
Its only protection is the access control above, so anything that can reach the app with a valid cookie can
run commands on the computer. Close terminals you are not using (up to 8 can be open at once). The Terminal tab
needs a pseudo-terminal, which Python provides on macOS and Linux only; on Windows the line-based Shell panel
is available instead.

The Shell panel is more limited: it runs one command at a time, applies the shell guard (destructive commands
and credential access are refused) and, when enabled, the Bash sandbox. As with the Bash tool, the guard reads
command text only; see [Shell guard and the sandbox](architecture.md#shell-guard-and-the-sandbox).

## Known limits

- The app is in development; see the status note above.
- If a tab shows "signed out", run `axon open` or `/open` again for a fresh link.
- A page attached to a running terminal session cannot change folder; run `axon open --workspace DIR` for
  another project.
