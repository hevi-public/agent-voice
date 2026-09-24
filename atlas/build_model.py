"""Builds the Atlas model: agent-voice's modules, functions, calls and I/O seams.

Line numbers and call edges come from the source (ast), coverage from a
pytest-cov JSON report, history from git; descriptions, tags, I/O and tours
are curated below. It refuses to build when a function has no annotation or an
annotation names a function that is gone, so the map can't silently drift from
the code.

    uv run --with pytest-cov pytest -q --cov=agent_voice --cov-report=json:atlas/coverage.json
    uv run python atlas/build_model.py
"""

import ast
import json
import pathlib
import subprocess
import sys

ATLAS = pathlib.Path(__file__).resolve().parent
REPO = ATLAS.parent
SRC = REPO / "src/agent_voice"
OUT = ATLAS / "model.json"
COVERAGE = ATLAS / "coverage.json"


def git(*args):
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=True).stdout.strip()


COMMIT = git("rev-parse", "--short", "HEAD")

# ---------------------------------------------------------------- lenses

LENSES = [
    {"id": "security", "label": "Security", "q": "What touches security?"},
    {"id": "privacy", "label": "Privacy", "q": "What is never spoken or logged?"},
    {"id": "network", "label": "Network", "q": "What touches the network?"},
    {"id": "audio", "label": "Audio", "q": "What makes sound?"},
    {"id": "files", "label": "Files & config", "q": "What reads or writes files?"},
    {"id": "process", "label": "Processes", "q": "What starts or talks to another process?"},
    {"id": "resilience", "label": "Fallbacks", "q": "What keeps working when something fails?"},
    {"id": "performance", "label": "Speed", "q": "What makes it fast?"},
    {"id": "agent", "label": "Agent-facing", "q": "What do Claude Code and Copilot touch?"},
]

# ---------------------------------------------------------------- outside world

EXTERNALS = [
    # id, label, sub, side, summary, detail, tags — ROLES below says, in plain words, what each does for agent-voice
    ("ext.user", "You", "terminal", "left",
     "Runs agent-voice commands and hears the result.",
     "Installs with uv, runs install-skill / install-hooks, mutes for meetings, reads `doctor`. Approves and answers agent prompts at the keyboard — which is why alerts are chimes by default.",
     ["agent"]),
    ("ext.claude", "Claude Code", "agent", "left",
     "Loads the skill, runs `agent-voice say`, fires hooks.",
     "Reads SKILL.md from ~/.claude/skills and calls `agent-voice say` through its Bash tool. Its PermissionRequest hook fires when an approval dialog opens — its question tool (AskUserQuestion) goes through it too. With --speak, PostToolUse, PostToolUseFailure, UserPromptSubmit and Stop fire the cancels. Hooks get the event as JSON on stdin and treat stdout as a verdict.",
     ["agent"]),
    ("ext.copilot", "Copilot CLI", "agent", "left",
     "Loads the skill, runs `agent-voice say`, fires notification hooks.",
     "Reads SKILL.md from ~/.copilot/skills. Its `notification` hook carries notification_type permission_prompt (approval) or elicitation_dialog (assumed to be a question — undocumented, unconfirmed). Its payloads don't name their event, so installed commands pass --event.",
     ["agent"]),
    ("ext.vscode", "Copilot in VS Code", "agent", "left",
     "Loads the skill only.",
     "Reads ~/.claude/skills and ~/.copilot/skills (so it lists the skill twice). Has no approval event, so gets no hooks.",
     ["agent"]),
    ("ext.install", "uv + GitHub", "install time", "left",
     "`uv tool install git+https://github.com/hevi-public/agent-voice`.",
     "Installs into ~/.local/share/uv/tools/agent-voice, links ~/.local/bin/agent-voice. About 1 GB of packages. Installs from git, not PyPI, because the spaCy model is a direct-URL dependency, which PyPI rejects.",
     ["network", "files"]),
    ("ext.audio", "macOS audio", "afplay · say", "right",
     "/usr/bin/afplay plays WAVs and chimes; /usr/bin/say is the fallback voice.",
     "Every sound leaves through a subprocess: afplay for Kokoro's WAV and for the Glass/Ping chimes, say when Kokoro can't run.",
     ["audio", "process"]),
    ("ext.state", "~/.agent-voice", "state folder", "right",
     "playback.lock · server.lock · voice.sock · muted · hook-log files.",
     "AGENT_VOICE_HOME moves it. playback.lock serialises sound across processes; server.lock keeps one server; voice.sock (0600) is the server's socket, removed when it quits; muted exists only while muted; hook-log-on / hooks.log only while logging.",
     ["files", "security"]),
    ("ext.config", "Agent config", "~/.claude · ~/.copilot", "right",
     "settings.json (edited!), skills folders, Copilot hook file.",
     "The only file agent-voice edits rather than owns is ~/.claude/settings.json — merged atomically, backed up once, left untouched when it isn't valid JSON. Everything else here is agent-voice's own file.",
     ["files", "security", "agent"]),
    ("ext.hf", "Hugging Face", "hub + ~/.cache", "right",
     "Model download — only in `prefetch`.",
     "mlx-community/Kokoro-82M-bf16, about 340 MB, cached in ~/.cache/huggingface/hub. Speaking reads the cache only and sets HF_HUB_OFFLINE, so nothing contacts the network after the download.",
     ["network", "files"]),
    ("ext.mlx", "mlx-audio · Kokoro", "neural TTS", "right",
     "Kokoro-82M on Apple Silicon via MLX.",
     "load_model() builds the model once per process (the voice server keeps it warm); model.generate() yields audio segments. A cold first sentence costs ~3.5 s, a warm one ~0.35 s.",
     ["audio", "performance"]),
    ("ext.g2p", "spaCy · espeak-ng", "misaki: text → phonemes", "right",
     "English pronunciation, with espeak for unknown words.",
     "misaki needs spaCy's en_core_web_sm (not on PyPI → direct wheel URL). espeak-ng keeps its data path in a 160-byte buffer: a real path over 159 bytes kills the whole process, hence the guard.",
     ["resilience"]),
    ("ext.sf", "soundfile", "WAV writer", "right",
     "Writes the samples to a WAV.",
     "Used for the temp WAV that afplay plays and for `say --out`.",
     ["files"]),
]

ROLES = {'ext.user': 'installs it, runs its commands, hears it',
    'ext.claude': 'reads the skill, speaks, sends hook events',
    'ext.copilot': 'reads the skill, speaks, sends hook events',
    'ext.vscode': 'reads the skill; no hooks',
    'ext.install': 'installs the agent-voice command',
    'ext.audio': 'plays the speech and the chimes',
    'ext.state': 'locks, the server socket, mute flag',
    'ext.config': 'skills, hook entries, settings.json',
    'ext.hf': 'the model download (prefetch only)',
    'ext.mlx': 'turns text into speech',
    'ext.g2p': 'works out how words are pronounced',
    'ext.sf': 'writes the WAV file'}

# ---------------------------------------------------------------- modules

MODULES = [
    # id, file, label, summary, detail, tags
    ("cli", "cli.py", "cli.py", "The `agent-voice` command: parses a subcommand and hands it to the module that does the work.",
     "Also the `hook` entry point the agents call, which always exits 0 and prints nothing: an agent reads a hook's output as a verdict. No bare `speak` alias — Homebrew's espeak-ng owns that name.",
     ["agent"]),
    ("hooks", "hooks.py", "hooks.py", "Turns agent hook events into chimes or spoken alerts, cancels them, and installs the hook entries.",
     "Stateless per event. Default: a chime (Glass = approval, Ping = question). With --speak: a sentence naming the tool kind and project, queued on the voice server and cancelled when the agent moves on.",
     ["agent", "security"]),
    ("skill", "skill.py", "skill.py", "Installs SKILL.md where Claude Code and Copilot look for skills; prints the always-on instruction.",
     "Never creates ~/.claude or ~/.copilot; keeps a SKILL.md you've edited unless --force.",
     ["agent", "files"]),
    ("skillmd", "skill/SKILL.md", "SKILL.md", "What the agent reads: when to speak, how, and the two modes.",
     "Announcer (default) speaks when done, blocked or failed; conversational speaks every reply. The mode is a word after `/agent-voice`, read from the invocation itself — no placeholder syntax — so it works in both agents. Never speak secrets; paths only when asked.",
     ["agent", "privacy"]),
    ("server", "server.py", "server.py", "The voice server: a background process that keeps Kokoro warm, plus its client.",
     "Unix socket (0600) in ~/.agent-voice, one JSON line each way. One speaker at a time; tagged requests can be cancelled; quits after 30 idle minutes and removes its socket.",
     ["performance", "process"]),
    ("speech", "speech.py", "speech.py", "Text-to-speech core: settings, model loading, synthesis, playback and fallbacks.",
     "Everything that touches Kokoro. Heavy imports are lazy, so the CLI and hooks start fast. Falls back to macOS `say` whenever Kokoro can't produce audio.",
     ["audio", "resilience"]),
    ("init", "__init__.py", "Library API", "`from agent_voice import say, save, synthesize, prefetch …`",
     "Re-exports speech.py for colleagues' projects. The library keeps Kokoro in the caller's own process rather than using the voice server.",
     []),
]

# ---------------------------------------------------------------- functions
# key: "module.qualname" -> (summary, tags, io[(kind, text)], tests[])

F = {}


def fn(key, summary, tags=(), io=(), tests=()):
    F[key] = {"summary": summary, "tags": list(tags), "io": [list(x) for x in io], "tests": list(tests)}


# cli
fn("cli.main", "Builds the argument parser for every subcommand and runs the chosen one; turns any exception into `agent-voice: …` and exit 1.", ["agent"],
   [("process", "argv in, exit code out")])
fn("cli._add_say_arguments", "The options `say` takes: text, --voice, --speed, --out, --no-fallback, -v.")
fn("cli._read_text", "Text from the arguments, else from stdin (so quotes, $ and ! survive).", [], [("stdin", "reads stdin when no text given")],
   ["test_text_comes_from_stdin_when_no_arguments"])
fn("cli._run_say", "`agent-voice say`: sets HF_HUB_OFFLINE, checks mute, tries the voice server, else speaks in-process.", ["network", "privacy"],
   [("env", "sets HF_HUB_OFFLINE=1")],
   ["test_arguments_are_joined", "test_nothing_to_say_is_a_usage_error", "test_failure_exits_nonzero_with_a_message", "test_say_turns_hugging_face_offline"])
fn("cli._say_via_server", "Hands the text to the voice server; False when there is none (the caller then speaks in-process). -v bypasses it.", ["performance", "resilience"], [],
   ["test_say_goes_through_the_voice_server_when_there_is_one", "test_say_speaks_in_process_without_a_server", "test_a_server_error_is_reported"])
fn("cli._prefetch", "`agent-voice prefetch`: the one command that downloads — the model, then one test sentence.", ["network"])
fn("cli._voices", "`agent-voice voices`: lists the English voices in the cached model.")
fn("cli._harnesses", "The --for choices, or all harnesses.")
fn("cli._install_skill", "`install-skill`: installs SKILL.md, then prints each installed agent's always-on instruction.", ["agent"], [],
   ["test_cli_install_skill_into_a_project", "test_cli_install_skill_fails_without_any_harness", "test_instruction_steps_only_for_installed_agents"])
fn("cli._describe", "One line per install result: installed / updated / kept / skipped.")
fn("cli._uninstall_skill", "`uninstall-skill`: removes agent-voice's skill folders only.")
fn("cli._hook", "`agent-voice hook`: the command agents run on each event. Reads stdin, never prints, always exits 0.", ["agent", "security"],
   [("stdin", "hook event JSON from the agent")],
   ["test_cli_hook_is_silent_and_always_succeeds"])
fn("cli._install_hooks", "`install-hooks [--speak]`: installs chime (or spoken) hooks for each agent that's set up.", ["agent", "files"])
fn("cli._uninstall_hooks", "`uninstall-hooks`: removes exactly what install-hooks added.", ["agent", "files"])
fn("cli._serve", "`agent-voice serve`: runs the voice server in this process (normally started for you).", ["process", "performance"])
fn("cli._stop", "`agent-voice stop`: asks the server to quit and free its memory.", ["process"])
fn("cli._hook_log", "`hook-log on|show|off`: records which hook events arrive — names only.", ["privacy"], [("file", "shows ~/.agent-voice/hooks.log")])
fn("cli._mute", "`agent-voice mute`: creates the mute flag; every voice and chime goes silent.", ["audio"])
fn("cli._unmute", "`agent-voice unmute`: removes the flag (AGENT_VOICE_MUTE in a shell still mutes it).", ["audio"])
fn("cli._doctor", "`agent-voice doctor`: checks Apple Silicon, afplay, espeak path, spaCy model, cached model; shows mute, skill, hooks and server.", ["resilience"])

# hooks
fn("hooks._tool_action", "Tool name → spoken kind: Bash → 'run a shell command', mcp__… → 'use an external tool'. Never the tool's input.", ["privacy"], [], ["test_tool_kinds"])
fn("hooks._worktree_main", "Reads a git worktree's .git file to find its main checkout (or a bare repo).", ["files"], [("file", "reads <folder>/.git")],
   ["test_a_worktree_is_named_after_its_main_checkout", "test_a_relative_gitdir_is_followed", "test_a_bare_repositorys_worktree_drops_the_git_suffix", "test_a_submodule_keeps_its_own_name"])
fn("hooks.project_folder", "The folder a session is named after: the repo's top level (the main checkout's, for a worktree), else cwd.", ["files"], [("file", "walks up looking for .git")],
   ["test_a_subfolder_is_named_after_its_repository", "test_outside_git_the_folder_itself_is_used"])
fn("hooks._project", "The project's name made speakable; dropped when it wouldn't read aloud.", ["privacy"], [], ["test_an_unspeakable_folder_name_is_left_out"])
fn("hooks.prompt_kind", "'approval', 'question' or None: the only events that mean the agent waits on you.", ["agent"], [],
   ["test_other_events_are_ignored", "test_copilot_permission_prompt"])
fn("hooks.announcement", "The spoken sentence (--speak): 'Your approval is needed to run a shell command in …'. Never the command, never the agent's name.", ["privacy", "security"], [],
   ["test_claude_prompt_names_the_tool_kind_and_project_but_no_agent", "test_nothing_the_model_wrote_is_spoken", "test_a_question_is_announced_as_a_question"])
fn("hooks._chime", "The default alert: plays Glass (approval) or Ping (question) with a detached afplay.", ["audio", "process"],
   [("audio", "afplay /System/Library/Sounds/Glass|Ping.aiff")],
   ["test_an_approval_chimes_by_default", "test_a_question_has_its_own_chime", "test_the_chimes_exist", "test_muted_means_no_chime"])
fn("hooks._session", "The event's session id, if it's safe to use as a tag (letters, digits, - and _ only).", ["security"])
fn("hooks._tool_key", "The tool's name as a tag suffix — not its input, which grows between events (AskUserQuestion adds answers).", ["security"], [],
   ["test_answering_a_question_cancels_it_although_its_input_grew"])
fn("hooks._speak_in_background", "Queues the sentence on the voice server without waiting; without one, speaks from a detached process.", ["process", "resilience", "performance"],
   [("process", "spawns `python -m agent_voice say` if the server is unavailable")],
   ["test_without_a_server_it_speaks_from_a_detached_process", "test_muted_means_nothing_is_queued"])
fn("hooks._log_flag", "Path of the hook-log switch file.", ["files"])
fn("hooks.log_path", "Path of hooks.log.", ["files"])
fn("hooks.set_logging", "Turns hook logging on, or off and deletes the log.", ["privacy", "files"], [("file", "~/.agent-voice/hook-log-on, hooks.log")])
fn("hooks._log", "Appends event name, tool name and payload keys — never values — while logging is on.", ["privacy", "files"],
   [("file", "appends ~/.agent-voice/hooks.log")], ["test_hook_log_records_names_but_never_contents"])
fn("hooks.handle", "One hook event in: a cancel event stops a spoken alert; a prompt event chimes (or speaks with --speak). Never raises, never prints.", ["agent", "security"], [],
   ["test_hook_ignores_anything_else", "test_claude_announcement_is_queued_tagged_with_its_tool_call", "test_the_approved_tool_finishing_cancels_its_announcement",
    "test_moving_on_cancels_the_whole_session", "test_copilot_cancels_by_session_using_the_installed_event_name"])
fn("hooks.NoHarnessError", "Raised when no requested agent folder exists — nothing is written.", ["files"])
fn("hooks.Result", "One install/uninstall outcome.")
fn("hooks.executable", "Absolute path of agent-voice, because an agent started from the Dock may lack ~/.local/bin on PATH.", ["resilience"])
fn("hooks.hook_command", "The command line written into agent config: `<path> hook --from claude --event X [--speak]`.", ["agent"])
fn("hooks._quote", "Single-quotes the program path when it has spaces or shell characters.", ["security"], [],
   ["test_a_program_path_with_spaces_is_quoted_and_still_recognised"])
fn("hooks._is_ours", "Recognises agent-voice's own hook commands, so install/uninstall never touch anyone else's.", ["security"])
fn("hooks._paths", "Each agent's folder and the file its hooks live in.", ["files"])
fn("hooks._write_json", "Writes JSON atomically: temp file, then os.replace.", ["files", "security"], [("file", "write temp + rename")])
fn("hooks._read_settings", "Reads settings.json; invalid JSON stops the install and leaves the file untouched.", ["files", "security"],
   [("file", "reads ~/.claude/settings.json")], ["test_invalid_settings_are_left_untouched"])
fn("hooks._events", "Which events to hook: only the prompt event for a chime, plus the cancel events with --speak.", ["agent"], [],
   ["test_by_default_only_the_prompt_events_are_hooked"])
fn("hooks._our_claude_handlers", "Finds agent-voice's entries among all of settings.json's hooks.", ["security"])
fn("hooks._install_claude", "Merges agent-voice's entries into ~/.claude/settings.json, keeping everything else; backs the file up once.", ["files", "security", "agent"],
   [("file", "edits ~/.claude/settings.json; copies settings.json.agent-voice-backup")],
   ["test_claude_hook_merges_into_existing_settings", "test_claude_hook_into_a_fresh_settings_file", "test_reinstalling_replaces_what_older_versions_added",
    "test_switching_back_from_speak_to_chime_removes_the_stop_hooks"])
fn("hooks._remove_claude_hooks", "Removes only agent-voice's handlers, and any group or event they leave empty.", ["security", "files"])
fn("hooks._copilot_file", "The whole content of ~/.copilot/hooks/agent-voice.json — its own file, so nothing of yours is edited.", ["agent", "files"], [],
   ["test_copilot_hook_gets_its_own_file"])
fn("hooks.install", "Installs hooks for each agent whose folder exists; fails without writing when none does.", ["files", "security"], [],
   ["test_no_agent_folder_is_an_error"])
fn("hooks.uninstall", "Removes exactly what install added.", ["files"])
fn("hooks.installed", "Which agents have agent-voice hooks, for `doctor`.")

# skill
fn("skill.instruction_steps", "Where each agent keeps always-on instructions (CLAUDE.md; ~/.copilot/instructions/agent-voice.instructions.md with applyTo) — printed, never written.", ["agent", "security"], [],
   ["test_copilot_instructions_go_in_a_file_it_loads"])
fn("skill.NoHarnessError", "Raised when no requested agent folder exists.")
fn("skill.Target", "An agent, its folder, and where its SKILL.md goes.")
fn("skill.Result", "One install/uninstall outcome.")
fn("skill.skill_text", "Reads the bundled SKILL.md from the package.", ["files"], [("file", "package resource skill/SKILL.md")],
   ["test_skill_has_the_frontmatter_both_harnesses_require", "test_the_skill_takes_a_mode_in_a_form_both_agents_pass_on"])
fn("skill.targets", "SKILL.md destinations: ~/.claude/skills & ~/.copilot/skills, or a repo's .claude/.github.", ["files", "agent"], [],
   ["test_personal_install_covers_claude_and_copilot", "test_project_install_uses_repo_folders"])
fn("skill.install", "Writes SKILL.md where the agent folder exists; keeps an edited one unless --force; never creates the agent's folder.", ["files", "security"],
   [("file", "writes <agent>/skills/agent-voice/SKILL.md")],
   ["test_a_missing_harness_folder_is_skipped_not_created", "test_no_harness_folder_at_all_is_an_error_and_writes_nothing", "test_edited_skill_is_kept_unless_forced"])
fn("skill.uninstall", "Deletes agent-voice's skill folders only.", ["files"], [("file", "rmtree <agent>/skills/agent-voice")], ["test_uninstall_removes_only_the_skill_folder"])

# server
fn("server.socket_path", "~/.agent-voice/voice.sock.", ["files"])
fn("server.enabled", "False when AGENT_VOICE_SERVER=0.", ["resilience"], [], ["test_disabled_means_no_server"])
fn("server.idle_timeout", "30 minutes, or AGENT_VOICE_IDLE.", ["performance"])
fn("server.Job", "One request: text, voice, speed, tag, and the outcome the waiting client reads.")
fn("server._matches", "Tag match: exact, or a prefix followed by ':' (a session cancels all its tools).", [], [], ["test_cancel_drops_queued_announcements_by_tag_and_prefix"])
fn("server.Voice", "The queue and its one worker.", ["performance"])
fn("server.Voice.__init__", "Injectable synthesize/player, so tests run the queue without Kokoro or speakers.")
fn("server.Voice.submit", "Queues a job and wakes the worker.")
fn("server.Voice.cancel", "Drops queued jobs with the tag and terminates the one playing.", ["audio"], [("process", "terminates the running afplay")],
   ["test_cancel_stops_what_is_playing"])
fn("server.Voice.stop", "Asks the worker to finish.")
fn("server.Voice.idle_for", "Seconds since the last activity, for ping and doctor.")
fn("server.Voice._next", "Waits for the next job, or returns None once idle for the timeout.", ["performance"], [], ["test_goes_idle_and_returns"])
fn("server.Voice.run", "The worker loop — runs on the main thread, where MLX is happiest.", ["performance"], [], ["test_speaks_in_order_and_reports_the_engine"])
fn("server.Voice._speak", "Synthesises with the warm model, writes a temp WAV and plays it under the playback lock; falls back to `say`.", ["audio", "resilience", "process"],
   [("audio", "afplay speech.wav, or say")], ["test_kokoro_failure_falls_back_to_say", "test_without_fallback_the_error_is_returned"])
fn("server._Handler", "One connection: one JSON request, one JSON reply.", ["process"])
fn("server._Handler.handle", "Reads the request line, answers, writes the reply.", ["process"], [("socket", "JSON line in / out")])
fn("server._Handler._answer", "say (wait or not) · cancel · ping · stop.", ["process"], [], ["test_protocol_round_trip"])
fn("server._Server", "A threaded Unix-socket server.", ["process"])
fn("server.serve", "Runs the server: takes server.lock (one instance), replaces a stale socket, binds 0600, warms Kokoro, works the queue, cleans up.", ["process", "security", "performance", "files"],
   [("file", "server.lock, voice.sock (0600)"), ("socket", "listens on voice.sock")],
   ["test_stop_shuts_down_and_removes_the_socket", "test_a_second_server_leaves_the_first_alone", "test_a_stale_socket_is_replaced"])
fn("server.request", "Client side: one JSON request over the socket; None when no server answers.", ["process"], [("socket", "connects to voice.sock")])
fn("server.usable", "Enabled, and the socket path fits in 104 bytes (macOS's limit).", ["resilience"], [], ["test_a_state_folder_too_deep_for_a_socket_means_no_server"])
fn("server.running", "Pings the server.", ["process"])
fn("server.start", "Starts a server in the background unless one answers; waits up to 10 s.", ["process", "performance"],
   [("process", "spawns `python -m agent_voice serve` (detached)")])
fn("server.speak", "Has the server speak, starting it if needed; None when there's no server to be had.", ["performance", "resilience"])
fn("server.cancel", "Stops a tag's announcements if a server runs — never starts one.", ["process"])
fn("server.stop", "Asks the server to quit.", ["process"])

# speech
fn("speech.VoiceError", "Speech couldn't be produced — the trigger for the `say` fallback.", ["resilience"])
fn("speech.state_dir", "~/.agent-voice, or AGENT_VOICE_HOME.", ["files"])
fn("speech._mute_flag", "Path of the mute flag.", ["files"])
fn("speech.is_muted", "AGENT_VOICE_MUTE, or the mute flag file.", ["audio"], [("file", "checks ~/.agent-voice/muted")], ["test_muted_says_nothing"])
fn("speech.set_muted", "Creates or removes the mute flag.", ["audio", "files"], [("file", "~/.agent-voice/muted")], ["test_unmute_removes_the_flag"])
fn("speech.default_voice", "af_heart, or AGENT_VOICE_VOICE.")
fn("speech.default_speed", "1.0, or AGENT_VOICE_SPEED.", [], [], ["test_bad_speed_env_is_reported"])
fn("speech.espeak_path_problem", "Refuses before espeak-ng can kill the process: its data path must fit in 159 bytes.", ["resilience"],
   [("file", "realpath of espeak-ng-data")],
   ["test_an_install_too_deep_for_espeak_falls_back_instead_of_dying", "test_a_normal_install_path_is_fine"])
fn("speech.lang_code", "Kokoro voice → language: af_/am_ American, bf_/bm_ British; anything else refused.", [], [],
   ["test_english_voices_map_to_their_language", "test_non_english_voice_is_rejected"])
fn("speech.model_path", "The cached Kokoro snapshot. Downloads only when asked (prefetch); speaking never does.", ["network", "files", "security"],
   [("network", "snapshot_download (prefetch only)"), ("file", "~/.cache/huggingface/hub")],
   ["test_speaking_never_downloads_the_model"])
fn("speech.prefetch", "Downloads the model if missing, then renders one sentence so a broken install fails here.", ["network"])
fn("speech._quiet", "Keeps mlx-audio's chatter off stdout and stderr.")
fn("speech._load_model", "Loads Kokoro once per process — the cost the voice server exists to pay once.", ["performance"], [("lib", "mlx_audio.tts.utils.load_model")])
fn("speech.synthesize", "Text → samples: guards espeak's path, picks the language, loads the model, runs Kokoro.", ["audio", "resilience"],
   [("lib", "model.generate (Kokoro + misaki/spaCy/espeak)")])
fn("speech._playback_lock", "flock on playback.lock: two agents take turns instead of talking over each other.", ["audio", "files"],
   [("file", "~/.agent-voice/playback.lock")])
fn("speech._write_wav", "Samples → WAV via soundfile.", ["files"], [("file", "temp WAV")])
fn("speech.play", "Writes a temp WAV and plays it with afplay under the lock (in-process path).", ["audio", "process"], [("audio", "afplay")],
   ["test_kokoro_output_is_played"])
fn("speech._say_fallback", "macOS `say`, spoken or rendered to a WAV.", ["audio", "resilience", "process"], [("audio", "say")],
   ["test_falls_back_to_say_when_kokoro_fails", "test_save_fallback_writes_a_wave_file"])
fn("speech._require_text", "Empty text is an error, not a fallback.", [], [], ["test_empty_text_is_an_error_not_a_fallback"])
fn("speech._warn_fallback", "Says on stderr why Kokoro wasn't used.", ["resilience"])
fn("speech.say", "Library/in-process speak: mute check, synthesize, play; falls back to `say` unless fallback=False.", ["audio", "resilience"], [],
   ["test_no_fallback_raises_the_original_error"])
fn("speech.save", "Like say, but writes a WAV file.", ["files"])
fn("speech.english_voices", "The English voices in the cached model.", ["files"])

# ---------------------------------------------------------------- extra edges (beyond ast calls)
# (from, to, kind, label)
EXTRA = [
    # agents and user into the package
    ("ext.user", "cli.main", "io", "runs agent-voice …"),
    ("ext.claude", "skillmd", "io", "loads the skill"),
    ("ext.copilot", "skillmd", "io", "loads the skill"),
    ("ext.vscode", "skillmd", "io", "loads the skill"),
    ("ext.claude", "cli._run_say", "io", "Bash: agent-voice say"),
    ("ext.copilot", "cli._run_say", "io", "shell: agent-voice say"),
    ("ext.claude", "cli._hook", "io", "hook event → stdin"),
    ("ext.copilot", "cli._hook", "io", "hook event → stdin"),
    ("ext.install", "cli.main", "io", "installs the command"),
    # processes and sockets
    ("server.request", "server._Handler.handle", "socket", "Unix socket · JSON line"),
    ("server.start", "cli._serve", "spawn", "spawns `agent-voice serve`"),
    ("hooks._speak_in_background", "cli._run_say", "spawn", "fallback: spawns `say`"),
    ("server._Handler._answer", "server.Voice.submit", "call", "queues the job"),
    ("server._Handler._answer", "server.Voice.cancel", "call", "cancel"),
    ("server.serve", "server.Voice.run", "call", "works the queue"),
    ("server.Voice._speak", "speech.synthesize", "call", "warm model"),
    ("server.Voice._speak", "speech._write_wav", "call", "temp WAV"),
    ("server.Voice._speak", "speech._playback_lock", "call", "takes turns"),
    ("skill.skill_text", "skillmd", "io", "reads"),
    ("skill.install", "skillmd", "io", "copies"),
    ("init", "speech.say", "call", "re-exports"),
    # to the outside world
    ("hooks._chime", "ext.audio", "io", "afplay Glass/Ping"),
    ("speech.play", "ext.audio", "io", "afplay WAV"),
    ("speech._say_fallback", "ext.audio", "io", "say"),
    ("server.Voice._speak", "ext.audio", "io", "afplay / say"),
    ("server.Voice.cancel", "ext.audio", "io", "terminates afplay"),
    ("speech._playback_lock", "ext.state", "io", "flock playback.lock"),
    ("speech.is_muted", "ext.state", "io", "reads muted"),
    ("speech.set_muted", "ext.state", "io", "writes muted"),
    ("server.serve", "ext.state", "io", "server.lock · voice.sock"),
    ("server.request", "ext.state", "io", "connects voice.sock"),
    ("hooks._log", "ext.state", "io", "appends hooks.log"),
    ("hooks.set_logging", "ext.state", "io", "hook-log flag"),
    ("hooks._install_claude", "ext.config", "io", "edits settings.json"),
    ("hooks._read_settings", "ext.config", "io", "reads settings.json"),
    ("hooks.install", "ext.config", "io", "writes Copilot hook file"),
    ("hooks.uninstall", "ext.config", "io", "removes entries / file"),
    ("skill.install", "ext.config", "io", "writes SKILL.md"),
    ("skill.uninstall", "ext.config", "io", "removes skill folder"),
    ("speech.model_path", "ext.hf", "io", "cache · download"),
    ("speech._load_model", "ext.mlx", "io", "load_model"),
    ("speech.synthesize", "ext.mlx", "io", "model.generate"),
    ("ext.mlx", "ext.g2p", "io", "phonemes"),
    ("speech.espeak_path_problem", "ext.g2p", "io", "checks data path"),
    ("speech._write_wav", "ext.sf", "io", "sf.write"),
]

EDGE_LABELS = {  # labels for selected ast call edges
    ("cli._run_say", "cli._say_via_server"): "try the server",
    ("cli._run_say", "speech.say"): "in-process",
    ("cli._say_via_server", "server.speak"): "speak",
    ("cli._hook", "hooks.handle"): "event",
    ("hooks.handle", "server.cancel"): "cancel tag",
    ("hooks._speak_in_background", "server.speak"): "queue (no wait)",
    ("server.speak", "server.start"): "start if needed",
    ("server.speak", "server.request"): "say",
    ("speech.synthesize", "speech._load_model"): "once per process",
}

# ---------------------------------------------------------------- tours

TOURS = [
    {"id": "voice", "title": "How is a voice generated?", "tags": ["audio", "performance"], "steps": [
        {"focus": "ext.claude", "level": 0, "hl": ["ext.claude", "cli._run_say"], "text": "An agent decides to speak and runs `agent-voice say \"…\"` through its shell tool — the skill told it when and how."},
        {"focus": "cli._run_say", "level": 2, "hl": ["cli.main", "cli._run_say", "cli._read_text"], "text": "cli.main routes to _run_say. It sets HF_HUB_OFFLINE so nothing reaches the network, reads the text (arguments or stdin) and checks mute."},
        {"focus": "cli._say_via_server", "level": 2, "hl": ["cli._say_via_server", "server.speak", "server.usable", "server.start"], "text": "_say_via_server hands the text to server.speak, which starts the voice server on first use (a detached `agent-voice serve`) and waits until it answers."},
        {"focus": "server.request", "level": 2, "hl": ["server.request", "server._Handler.handle", "server._Handler._answer", "server.Voice.submit"], "text": "The client sends one JSON line over ~/.agent-voice/voice.sock. In the server process, the handler turns it into a Job and queues it — one speaker at a time."},
        {"focus": "server.Voice._speak", "level": 2, "hl": ["server.Voice.run", "server.Voice._next", "server.Voice._speak", "speech.synthesize"], "text": "The worker (main thread, where MLX is happiest) takes the job and calls speech.synthesize with the model already warm."},
        {"focus": "speech.synthesize", "level": 2, "hl": ["speech.synthesize", "speech.espeak_path_problem", "speech.lang_code", "speech._load_model", "speech.model_path", "ext.mlx", "ext.g2p"], "text": "synthesize guards espeak's 159-byte path limit, maps the voice to a language, loads Kokoro once (cache only — never downloads) and runs model.generate: misaki and spaCy turn text into phonemes, espeak fills in unknown words."},
        {"focus": "speech._write_wav", "level": 2, "hl": ["speech._write_wav", "speech._playback_lock", "ext.sf", "ext.audio", "server.Voice._speak"], "text": "The samples become a temp WAV (soundfile), and afplay plays it under playback.lock so two agents never talk over each other. Warm, sound starts ~0.4 s after the request."},
        {"focus": "server._Handler._answer", "level": 1, "hl": ["server", "cli", "speech"], "text": "The reply ({engine: kokoro}) goes back to the waiting `say`, which exits. If anything failed, macOS `say` spoke instead — see “What if Kokoro fails?”."},
    ]},
    {"id": "approval", "title": "What happens when an agent needs approval?", "tags": ["agent", "audio"], "steps": [
        {"focus": "ext.claude", "level": 0, "hl": ["ext.claude", "ext.copilot"], "text": "An approval dialog (or a question) opens. Claude Code fires PermissionRequest; Copilot CLI fires a notification with permission_prompt or elicitation_dialog."},
        {"focus": "cli._hook", "level": 2, "hl": ["cli._hook", "hooks.handle"], "text": "The agent runs the installed command `agent-voice hook --from claude --event PermissionRequest` with the event as JSON on stdin. It never prints and always exits 0 — stdout would be read as a verdict on the tool call."},
        {"focus": "hooks.handle", "level": 2, "hl": ["hooks.handle", "hooks._log", "hooks.prompt_kind"], "text": "handle logs the event name if hook-log is on (names only), then prompt_kind decides: approval, question, or nothing to do."},
        {"focus": "hooks._chime", "level": 2, "hl": ["hooks._chime", "ext.audio", "speech.is_muted"], "text": "By default it chimes — Glass for an approval, Ping for a question — with a detached afplay, then returns in ~50 ms. You have to come to the keyboard anyway; a chime says enough."},
    ]},
    {"id": "speak", "title": "Spoken alerts and cancelling (--speak)", "tags": ["agent", "process"], "steps": [
        {"focus": "hooks.announcement", "level": 2, "hl": ["hooks.announcement", "hooks._tool_action", "hooks._project", "hooks.project_folder", "hooks._worktree_main"], "text": "With --speak, announcement builds the sentence: the tool kind from the agent's own tool name and the project's name (the main repo's, even from a worktree). Never the command — a partial read-out would make the rest sound safe."},
        {"focus": "hooks._speak_in_background", "level": 2, "hl": ["hooks._speak_in_background", "hooks._session", "hooks._tool_key", "server.speak"], "text": "It's queued on the voice server without waiting, tagged session:tool. No server? A detached `say` process speaks it instead."},
        {"focus": "server.cancel", "level": 2, "hl": ["hooks.handle", "server.cancel", "server._Handler._answer", "server.Voice.cancel", "server._matches"], "text": "Neither agent reports that a prompt was answered, so the next event does: the tool finishing, you typing, or the turn ending. handle sends cancel(tag); the server drops it from the queue or terminates the playing afplay."},
        {"focus": "hooks._events", "level": 2, "hl": ["hooks._events", "hooks._install_claude", "hooks._copilot_file"], "text": "That's why --speak installs extra hooks (PostToolUse, UserPromptSubmit, Stop …) and the chime default doesn't: a chime is over in a second."},
    ]},
    {"id": "settings", "title": "How install-hooks edits settings.json safely", "tags": ["security", "files"], "steps": [
        {"focus": "hooks.install", "level": 2, "hl": ["cli._install_hooks", "hooks.install", "hooks._paths"], "text": "install checks each agent's folder. A missing ~/.claude or ~/.copilot is skipped, never created; if none exists it fails before writing anything."},
        {"focus": "hooks._read_settings", "level": 2, "hl": ["hooks._install_claude", "hooks._read_settings", "ext.config"], "text": "settings.json is the one file agent-voice edits rather than owns. If it isn't valid JSON, the install stops and leaves it untouched."},
        {"focus": "hooks._remove_claude_hooks", "level": 2, "hl": ["hooks._remove_claude_hooks", "hooks._our_claude_handlers", "hooks._is_ours"], "text": "Old agent-voice entries (from any version) are removed first — only ours, recognised by the command itself; your own hooks stay."},
        {"focus": "hooks.hook_command", "level": 2, "hl": ["hooks.hook_command", "hooks.executable", "hooks._quote"], "text": "The new command uses agent-voice's absolute path (an agent launched from the Dock may not have ~/.local/bin on PATH), quoted if it contains spaces."},
        {"focus": "hooks._write_json", "level": 2, "hl": ["hooks._write_json", "hooks._install_claude", "ext.config"], "text": "A backup is copied once (settings.json.agent-voice-backup), then the file is written atomically: temp file, then rename. Copilot gets its own file, so nothing of yours is edited there."},
    ]},
    {"id": "network", "title": "When does it touch the network?", "tags": ["network"], "steps": [
        {"focus": "ext.install", "level": 0, "hl": ["ext.install"], "text": "Install time: uv fetches the package from GitHub and ~1 GB of dependencies, including the spaCy model by direct URL — the reason it isn't on PyPI."},
        {"focus": "speech.model_path", "level": 2, "hl": ["cli._prefetch", "speech.prefetch", "speech.model_path", "ext.hf"], "text": "`agent-voice prefetch` is the only runtime path that downloads: model_path(download=True) fetches Kokoro (~340 MB) from Hugging Face into its cache."},
        {"focus": "cli._run_say", "level": 2, "hl": ["cli._run_say", "speech.model_path", "speech._load_model"], "text": "Speaking reads the cache only (local_files_only) and `say` sets HF_HUB_OFFLINE, so huggingface_hub and transformers refuse to try. Tested with the network blocked."},
    ]},
    {"id": "fallback", "title": "What if Kokoro fails?", "tags": ["resilience"], "steps": [
        {"focus": "server.usable", "level": 2, "hl": ["server.usable", "server.enabled", "server.start", "cli._say_via_server"], "text": "No voice server (AGENT_VOICE_SERVER=0, a socket path over 104 bytes, or it won't start)? `say` speaks in-process instead."},
        {"focus": "speech.espeak_path_problem", "level": 2, "hl": ["speech.espeak_path_problem", "ext.g2p"], "text": "Installed too deep for espeak-ng? That would kill the process silently, so it's refused up front with a VoiceError that names the fix."},
        {"focus": "speech._say_fallback", "level": 2, "hl": ["speech.say", "speech._say_fallback", "speech._warn_fallback", "server.Voice._speak", "ext.audio"], "text": "Any Kokoro failure — missing model, bad voice, espeak — falls back to macOS `say`, and stderr says why. `--no-fallback` raises instead."},
        {"focus": "hooks._speak_in_background", "level": 2, "hl": ["hooks._speak_in_background"], "text": "Hooks degrade the same way: no server → a detached process speaks. And every hook exits 0 whatever happens, so the agent is never affected."},
    ]},
    {"id": "skill", "title": "How does the agent learn to speak?", "tags": ["agent"], "steps": [
        {"focus": "skill.install", "level": 2, "hl": ["cli._install_skill", "skill.targets", "skill.install", "skill.skill_text"], "text": "install-skill copies the bundled SKILL.md into ~/.claude/skills and ~/.copilot/skills — only where the agent's folder exists, and never over a copy you've edited."},
        {"focus": "skillmd", "level": 1, "hl": ["skillmd", "ext.claude", "ext.copilot", "ext.vscode"], "text": "The agent loads SKILL.md when its description matches — or when you type /agent-voice conversational or /agent-voice announcer. The mode word is read from the invocation, so no agent-specific syntax is needed."},
        {"focus": "skill.instruction_steps", "level": 2, "hl": ["skill.instruction_steps"], "text": "To make every session use it, install-skill prints an always-on line for CLAUDE.md and a Copilot instructions file — printed, never written: they're your files."},
    ]},
    {"id": "lifecycle", "title": "The voice server's life", "tags": ["process", "performance"], "steps": [
        {"focus": "server.start", "level": 2, "hl": ["server.start", "server.running", "cli._serve"], "text": "Born on the first `say`: start spawns a detached `agent-voice serve` and waits until it answers a ping."},
        {"focus": "server.serve", "level": 2, "hl": ["server.serve", "ext.state"], "text": "serve takes server.lock (a second server just exits), replaces a stale socket, binds voice.sock with mode 0600 and warms Kokoro before the first request."},
        {"focus": "server.Voice._next", "level": 2, "hl": ["server.Voice.run", "server.Voice._next", "server.idle_timeout"], "text": "It speaks one job at a time for as long as requests come. After 30 idle minutes (AGENT_VOICE_IDLE) the loop ends, freeing ~780 MB."},
        {"focus": "server.stop", "level": 2, "hl": ["server.stop", "cli._stop", "cli._doctor"], "text": "`agent-voice stop` ends it at once; `doctor` shows whether it runs. On exit it removes its socket — only the two lock files stay."},
    ]},
]

# ---------------------------------------------------------------- build


def ast_index():
    index = {}
    calls = []
    for path in sorted(SRC.glob("*.py")):
        mod = path.stem
        if mod.startswith("__"):
            continue
        tree = ast.parse(path.read_text())

        def visit(node, prefix=""):
            for n in getattr(node, "body", []):
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    key = f"{mod}.{prefix}{n.name}"
                    index[key] = {"file": f"src/agent_voice/{path.name}", "line": n.lineno, "end": n.end_lineno,
                                  "kind": "class" if isinstance(n, ast.ClassDef) else "function"}
                    if isinstance(n, ast.ClassDef):
                        visit(n, prefix + n.name + ".")
                    else:
                        found = set()
                        for c in ast.walk(n):
                            if isinstance(c, ast.Call):
                                f = c.func
                                if isinstance(f, ast.Name):
                                    found.add(("", f.id))
                                elif isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name):
                                    found.add((f.value.id, f.attr))
                        calls.append((key, mod, prefix, found))
        visit(tree)
    edges = []
    for key, mod, prefix, found in calls:
        for base, name in found:
            candidates = []
            if base == "":
                candidates = [f"{mod}.{name}"]
            elif base == "self" and prefix:
                candidates = [f"{mod}.{prefix}{name}"]
            elif base in ("speech", "server", "hooks", "skill"):
                candidates = [f"{base}.{name}"]
            for target in candidates:
                if target in index and target != key and index[target]["kind"] == "function":
                    edges.append((key, target))
    return index, sorted(set(edges))


def main():
    index, call_edges = ast_index()
    missing = sorted(set(index) - set(F))
    stale = sorted(set(F) - set(index))
    if missing or stale:
        sys.exit(f"annotations out of date — missing: {missing}  stale: {stale}")
    nodes = []
    for eid, label, sub, side, summary, detail, tags in EXTERNALS:
        nodes.append({"id": eid, "kind": "external", "label": label, "sub": sub, "side": side, "role": ROLES[eid],
                      "summary": summary, "detail": detail, "tags": tags})
    coverage = json.loads(COVERAGE.read_text())["files"] if COVERAGE.exists() else {}
    for mid, file, label, summary, detail, tags in MODULES:
        path = f"src/agent_voice/{file}"
        lines = len((REPO / path).read_text().splitlines())
        history = [dict(zip(("hash", "date", "subject"), line.split("|", 2)))
                   for line in git("log", "--format=%h|%ad|%s", "--date=short", "--", path).splitlines()]
        summary_cov = coverage.get(path, {}).get("summary")
        nodes.append({"id": mid, "kind": "module", "label": label, "file": path, "lines": lines,
                      "summary": summary, "detail": detail, "tags": tags, "history": history,
                      "cover": round(summary_cov["percent_covered"]) if summary_cov else None})
    for key, info in index.items():
        mod = key.split(".")[0]
        qual = key.split(".", 1)[1]
        depth = qual.count(".")
        ann = F[key]
        cov = coverage.get(info["file"], {}).get("functions" if info["kind"] == "function" else "classes", {}).get(qual)
        nodes.append({
            "id": key, "kind": info["kind"], "module": mod, "label": qual + ("()" if info["kind"] == "function" else ""),
            "depth": depth, "file": info["file"], "line": info["line"], "end": info["end"],
            "summary": ann["summary"], "tags": ann["tags"], "io": ann["io"], "tests": ann["tests"],
            "cover": round(cov["summary"]["percent_covered"]) if cov and cov["summary"]["num_statements"] else None,
        })
    edges = []
    seen = set()
    for a, b in call_edges:
        edges.append({"from": a, "to": b, "kind": "call", "label": EDGE_LABELS.get((a, b), "")})
        seen.add((a, b))
    ids = {n["id"] for n in nodes}
    for a, b, kind, label in EXTRA:
        if a not in ids or b not in ids:
            sys.exit(f"edge to unknown node: {a} -> {b}")
        if (a, b) in seen:
            for e in edges:
                if e["from"] == a and e["to"] == b:
                    e["label"] = label or e["label"]
            continue
        edges.append({"from": a, "to": b, "kind": kind, "label": label})
        seen.add((a, b))
    for tour in TOURS:
        for step in tour["steps"]:
            for i in [step["focus"], *step["hl"]]:
                if i not in ids:
                    sys.exit(f"tour {tour['id']} names unknown node {i}")
    tests_total = sum(1 for p in (REPO / "tests").glob("test_*.py") for line in p.read_text().splitlines() if line.startswith("def test_"))
    model = {
        "repo": "https://github.com/hevi-public/agent-voice", "commit": COMMIT,
        "lenses": LENSES, "nodes": nodes, "edges": edges, "tours": TOURS,
        "stats": {"functions": sum(1 for n in nodes if n["kind"] in ("function", "class")), "tests": tests_total,
                  "edges": len(edges), "coverage": round(json.loads(COVERAGE.read_text())["totals"]["percent_covered"]) if COVERAGE.exists() else None},
    }
    OUT.write_text(json.dumps(model, indent=1))
    print(f"{len(nodes)} nodes, {len(edges)} edges, {len(TOURS)} tours, {tests_total} tests -> {OUT}")


main()
