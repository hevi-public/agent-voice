---
name: agent-voice
description: Speak to the user aloud with the `agent-voice say` command. Two modes, switched with `/agent-voice announcer` or `/agent-voice conversational`. Announcer (the default) speaks a short update when you finish a task or a significant step, when you are blocked and need the user's input or approval, or when something failed that they must act on. Conversational speaks every reply, for a user who is talking to you by voice.
argument-hint: "[announcer | conversational]"
---

# Speaking to the user

The user works away from the screen, or talks to you by voice. Terminal text
goes unnoticed; a spoken sentence does not. Speak with:

```bash
agent-voice say "The login bug is fixed and the tests pass. Nothing needs your attention."
```

It blocks for the few seconds it takes to talk, then exits. Kokoro, a local
neural voice, does the speaking; if it cannot, macOS `say` takes over by itself.

## Modes

There are two modes, and **announcer** is the default.

To switch, the user invokes this skill with the mode's name:
`/agent-voice conversational` or `/agent-voice announcer`. The word may reach
you after the command, or as `ARGUMENTS: conversational`. When you switch:

1. Confirm it aloud in a few words, for example *"Conversational mode on."*
   or *"Back to announcements only."*
2. Stay in that mode for the rest of the session, until the user switches again.

If the skill loads without a mode word, keep the current mode. If you can't
tell which mode is on (after a long session, say), use announcer.

### Announcer

Speak once, at the end of a turn, and only when it matters:

- **Done:** a task or significant step is finished. Say what changed and what,
  if anything, the user should do next.
- **Blocked:** you need a decision, an approval or information. Say what you
  need, in one sentence.
- **Failed:** something broke that the user has to act on.

Do not speak for routine progress, for every step, or to read out your whole
answer.

### Conversational

The user is talking to you, probably through speech-to-text, and listening
rather than reading. Speak **every** reply:

- At the end of each turn, say your reply as you would in conversation. A short
  answer is spoken whole. A long one is spoken as its gist in a few sentences,
  ending with something like "the details are on screen".
- Before work that will take more than a minute, say one short sentence about
  what you're about to do, so the silence isn't confusing.
- Ask one question at a time, aloud: a listener can't scan a list of five.
- Speech-to-text gets words wrong: homophones, names, identifiers, missing
  punctuation. Read the user's words charitably. Where a misheard word would
  change what you do (which file, which branch, whether to delete or push),
  ask aloud instead of guessing.

## How to write what you say

These apply in both modes.

- Conversational sentences: what you'd say to a colleague over your shoulder.
- No markdown, code, file paths, URLs, hashes or long identifiers: they sound
  like noise. Describe them instead ("the config file", "the payment tests").
- Never speak secrets, tokens, credentials or personal data. Audio carries
  across a room and into calls.
- Always write the full answer as text too. The voice goes with the text; it
  doesn't replace it.
- If the message contains quotes, `$`, backticks or `!`, pass it on stdin so the
  shell leaves it alone:

  ```bash
  agent-voice say <<'EOF'
  The "retry" flag now defaults to off. Can you confirm that's what you want?
  EOF
  ```

## If it does not work

If `agent-voice` is not found or exits with an error, carry on without it. Do
not retry, install anything, or tell the user about it more than once. When the
user has muted it (`agent-voice mute`), it returns silently; nothing to do.
