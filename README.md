# LinkedIn reply-only bot

Local Python + Playwright automation for accepting incoming connection invitations
and replying to newly incoming messages. Runs in a visible browser with a dedicated
persistent profile. Login and any verification are done manually by the owner.

**Status:** code and logic checks completed; browser integration tests could not run
in the build environment because browser startup was restricted. Not yet validated
against a live LinkedIn account. Run the check and a real smoke test before submitting.
LinkedIn may change its DOM or restrict automation; this project does not bypass
security checks. Use LinkedIn's English interface for invitation button matching.

## Windows setup (PowerShell, Python 3.10+)

Extract this project, open its folder in VS Code, and open the terminal there:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
Copy-Item .env.example .env
```

Edit `.env` with your own LinkedIn profile URL. Configure `LLM_URL`, `LLM_MODEL`,
and `LLM_API_KEY` using an OpenAI-compatible provider or local server you choose.
`LLM_URL` must be the complete chat-completions endpoint. The model receives the
last 20 visible messages (up to 12,000 characters per message), so choose a provider
you trust. No message content is saved by this application or printed to the console.
The browser's own local cache may retain content. Without AI, replies use basic
English templates and will perform poorly on unusual or non-English messages.

```powershell
.\.venv\Scripts\python.exe bot.py --check
```

Log into LinkedIn in the opened browser. Open the inbox, then press Enter **in the
terminal**. The check reports incoming/outgoing/unknown message counts and composer
availability without accepting invitations or sending messages. For an empty inbox,
ask a friend to send a test message first. A valid thread should have `unknown: 0`
and `composer=1`. This only verifies extraction, not the Send/Accept buttons.

```powershell
.\.venv\Scripts\python.exe bot.py
```

Press Enter in the terminal after confirming login. Wait for `Running.` before
asking someone to send a **new** message and connection request. Existing inbox
messages are baselined on first startup so it does not reply to old conversations.
Verify actual acceptance and reply delivery from the sender's account. Then submit.
Do not operate the bot's browser while it runs; use a separate regular browser.
Keep the PC awake, connected, and the terminal/browser open until **Test finished**.
Ctrl+C stops it. No Docker or virtualization is needed.

## Behavior

- Checks invitations every 5 seconds, plus page load/action time, independently of AI replies.
- Sweeps up to 40 visible recent inbox threads. Full sweep time depends on thread count.
- Replies only to verified incoming messages; never clicks Connect or starts conversations.
- Waits briefly for consecutive messages, then checks again after generating a reply.
- Uses visible conversation context. Doesn't open links, execute message instructions,
  or download/view attachments. Asks for details where needed.
- Stores hashed thread/event identifiers in `private/state.sqlite3` for restart deduplication.
- Confirms a visible outgoing reply after clicking Send. An uncertain send is held
  rather than blindly retried. Inspect that thread manually. Once the outgoing
  message appears, the hold clears. If no message was sent, stop the bot and remove
  that thread's row from the local SQLite database before restarting.
- Logs counts/status and exception class names, never message text, names or URLs.
- Never modifies a composer containing an existing draft.

## Limitations and troubleshooting

Selectors target LinkedIn's `msg-s-*` messaging UI. A different UI may require changes
to `extract.js`, `thread_links`, the composer/Send selector, or the Accept button
pattern in `bot.py`. `--check` cannot prove live Send/Accept functionality. The bot
requires sender profile links to distinguish incoming/outgoing messages; it skips
unknown identities. Group chats are not intentionally supported. Only the first
40 rendered inbox threads are scanned; archived, filtered, paginated, message
requests, and other hidden inboxes may be missed. Newly discovered threads at the
very first startup are baselined, including any test messages already waiting.
Messages arriving during a later sweep are handled on that sweep or the next one.

Login expiry, checkpoints, network failure or UI changes need owner attention.
Errors retry on the next sweep; login/security checks are never bypassed. A crash
between Send and confirmation may require the manual inspection described above.
Local duplicate detection is conservative, not a distributed exactly-once guarantee.
AI quality depends on the model. Fallback replies are limited and English-only.
Very long input is truncated for the model; attachment contents are unavailable.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Synthetic Playwright tests cover confirmed send, restart deduplication, read-only
check, no reply to self, unknown sender refusal, attachment/group extraction,
uncertain send handling, and fallback output. They don't verify LinkedIn's live UI.

## Public repository

Commit only source, tests, README and `.env.example`. `.gitignore` excludes `.env`,
`private/` (cookies/browser profile/database), virtual environments and logs.
Never upload the whole working folder through GitHub's web uploader: it can bypass
`.gitignore`. Review `git status` and `git diff --cached` before pushing.

## Short submission note (use after live verification)

Runs locally using Python and Playwright with manual login and a persistent browser
session. Accepts incoming invitations and replies only to new incoming DMs using a
configurable AI model, recent conversation context, message-burst grouping, and
local duplicate prevention. Install requirements and Chromium, configure `.env`,
then run `python bot.py`. Must remain running on my PC. Does not open links or read
attachments; cannot know personal facts beyond configured context. Hidden inboxes,
group chats, security checkpoints and future LinkedIn UI changes are limitations.
