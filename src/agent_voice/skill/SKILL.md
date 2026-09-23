---
name: agent-voice
description: Speak a short status update aloud with the `speak` command, so a user who is working on something else hears it. Use when you finish a task or a significant step, when you are blocked and need the user's input or approval, or when something failed that the user has to act on. Not for routine progress.
---

# Speaking updates aloud

The user runs you in the background while doing other work. Terminal text goes
unnoticed; a spoken sentence does not. Announce with:

```bash
speak "The login bug is fixed and the tests pass. Nothing needs your attention."
```

`speak` blocks for the few seconds it takes to talk, then exits. Kokoro, a local
neural voice, does the speaking; if it cannot, macOS `say` takes over by itself.

## When to speak

- **Done:** a task or significant step is finished. Say what changed and what,
  if anything, the user should do next.
- **Blocked:** you need a decision, an approval or information. Say what you
  need, in one sentence.
- **Failed:** something broke that the user has to act on.

Do not speak for routine progress, for every step, or to read out your whole
answer. One announcement per turn, at the end, is the norm.

## How to write it

- One to three sentences, conversational — what you would say to a colleague
  over your shoulder.
- No markdown, code, file paths, URLs, hashes or long identifiers: they sound
  like noise. Describe them instead ("the config file", "the payment tests").
- Never speak secrets, tokens, credentials or personal data. Audio carries
  across a room and into calls.
- Always write the full answer as text too. The voice is the headline, not a
  replacement.
- If the message contains quotes, `$`, backticks or `!`, pass it on stdin so the
  shell leaves it alone:

  ```bash
  speak <<'EOF'
  The "retry" flag now defaults to off. Can you confirm that's what you want?
  EOF
  ```

## If it does not work

If `speak` is not found or exits with an error, carry on without it. Do not
retry, install anything, or tell the user about it more than once. When the
user has muted it (`agent-voice mute`), `speak` returns silently; nothing to do.
