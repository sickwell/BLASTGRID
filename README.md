# BLASTGRID

<p align="center">
  <img src="banner.png" alt="BLASTGRID" width="720">
</p>

AI coding agents (Cursor, Antigravity, Claude Code, etc.) auto-install dozens of skills into your system. Over time they pile up: unused skills eat context window tokens on every request, the agent picks wrong or conflicting instructions from stale skills, and there is no way to see what is actually being used vs what is sitting there dead. With 50+ skills across multiple agents, manual cleanup is not realistic.

BLASTGRID tracks real skill usage, shows what is dead weight, and lets you clean up in bulk. One command to vault everything unused, one command to restore if needed.

Supports 12 agents: Antigravity, Claude Code, Cursor, Gemini CLI, Codex CLI, Kiro, GitHub Copilot, Windsurf, Junie (JetBrains), Roo Code, OpenCode, and the shared `.agents` standard. If a directory exists on your machine, BLASTGRID picks it up automatically.

## Install

```
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

After that: `blastgrid` to launch, or `./run.sh` (activates venv for you).

## How it works

1. On launch, BLASTGRID scans all agent skill directories for `SKILL.md` files, estimates token cost of each skill, and stores everything in a local SQLite database.

2. A background daemon tracks which skills are actually read by agents. It detects file access using filesystem timestamps (`atime`). When an agent reads a SKILL.md, the daemon logs it and increments the usage counter.

3. After running for a while, you see which skills are actively used and which are dead weight (ghosts). Press `A` to open AUTOPWN — enter a threshold (e.g. `5`), and every skill with fewer uses gets moved to a backup vault. Protected skills (tagged `SECURED` or `KEEP`) are excluded.

4. Vaulted skills can be restored at any time with `Z` or `blastgrid restore`.

### Tracking details

The core trick: APFS (macOS) and ext4 (Linux) with `relatime` only update `atime` when `atime < mtime`. The daemon sets `atime = mtime - 1 day` on each SKILL.md. When an agent reads the file, the kernel bumps `atime` — the daemon catches the change and logs it, then re-primes for the next read.

**macOS:**
- Without sudo — `atime-poll` backend. Polls every 1s using the priming trick above. Works on APFS out of the box.
- With sudo — `fs_usage` backend. Hooks into the kernel's file activity stream. Most precise, catches every read in real-time.

**Linux (Ubuntu, etc.):**
- With `inotify-tools` — `inotifywait` backend. Kernel-level `access` event monitoring, real-time. Install: `sudo apt install inotify-tools`.
- Without `inotify-tools` — falls back to `atime-poll`, same priming trick as macOS. Works on ext4 with `relatime` (Ubuntu default).
- If the disk is mounted with `noatime`, the daemon will detect this and suggest either installing inotify-tools or remounting with `relatime`.

## Quick start

```
blastgrid              # TUI with live dashboard
blastgrid daemon       # standalone monitoring with timeline
blastgrid stats        # quick summary
blastgrid top 100      # top 100 skills by usage
blastgrid agents       # detected agents and paths
blastgrid restore      # list / restore backed-up skills
blastgrid help         # all commands
```

## TUI

Four tabs, switch with `1` `2` `3` `4`:

- **Dashboard** — context budget bar, live activity chart (10 min), session usage tiers, agent breakdown
- **Armory** — full skill inventory with search, sorting by uses/tokens/agent, tagging
- **Hunt** — ghost tracker: unused skills on top (sorted by token weight), active below. Cleanup hub
- **Secured** — skills immune to AUTOPWN and mass actions

Key bindings:

| Key | Action |
|-----|--------|
| A | AUTOPWN — mass vault by usage threshold |
| S | Toggle SECURED (immune to mass actions) |
| Z | Restore all from vault |
| B | Vault selected skill |
| K / R / C | Tag keep / remove / clear |
| X / U / P | Mark / unmark / purge ghosts |
| ENTER | Skill detail modal |
| TAB | Cycle agent filter |
| F5 | Re-scan |

## Data

`~/.blastgrid/`:

| File | Purpose |
|------|---------|
| `blastgrid.db` | Skills, usage counts, tags (SQLite) |
| `vault/` | Backed-up skills (restorable) |
| `graveyard/` | Deleted skills |
| `usage.log` | JSON lines access log (10 MB rotation) |

## Supported agents

| Agent | Global skill path |
|-------|------------------|
| Antigravity | `~/.gemini/antigravity/skills/` |
| Claude Code | `~/.claude/skills/` |
| Cursor | `~/.cursor/skills/`, `~/.cursor/skills-cursor/` |
| Gemini CLI | `~/.gemini/skills/` |
| Codex CLI | `~/.codex/skills/` |
| Kiro | `~/.kiro/skills/` |
| GitHub Copilot | `~/.copilot/skills/` |
| Windsurf | `~/.codeium/windsurf/skills/` |
| Junie (JetBrains) | `~/.junie/skills/` |
| Roo Code | `~/.roo/skills/` |
| OpenCode | `~/.config/opencode/skills/` |
| Shared | `~/.agents/skills/` |

Run `blastgrid agents` to see what's detected on your machine.

## Requirements

Python 3.10+. Dependencies (textual, rich) install automatically.
