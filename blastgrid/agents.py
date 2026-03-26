from __future__ import annotations

import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentDef:
    id: str
    name: str
    color: str
    global_dirs: tuple[Path, ...]
    session_hint: str


HOME = Path.home()
IS_MAC = platform.system() == "Darwin"
IS_WIN = platform.system() == "Windows"

_AGENTS: list[AgentDef] = [
    AgentDef(
        id="antigravity",
        name="Antigravity",
        color="bright_cyan",
        global_dirs=(
            HOME / ".gemini" / "antigravity" / "skills",
        ),
        session_hint="fs_usage (macOS)",
    ),
    AgentDef(
        id="claude",
        name="Claude Code",
        color="bright_magenta",
        global_dirs=(
            HOME / ".claude" / "skills",
        ),
        session_hint="JSONL ~/.claude/projects/",
    ),
    AgentDef(
        id="cursor",
        name="Cursor",
        color="bright_green",
        global_dirs=(
            HOME / ".cursor" / "skills",
            HOME / ".cursor" / "skills-cursor",
        ),
        session_hint="rules injection",
    ),
    AgentDef(
        id="gemini",
        name="Gemini CLI",
        color="bright_yellow",
        global_dirs=(
            HOME / ".gemini" / "skills",
        ),
        session_hint="gemini skills list",
    ),
    AgentDef(
        id="codex",
        name="Codex CLI",
        color="bright_red",
        global_dirs=(
            HOME / ".codex" / "skills",
        ),
        session_hint="$skill invocation",
    ),
    AgentDef(
        id="kiro",
        name="Kiro",
        color="bright_white",
        global_dirs=(
            HOME / ".kiro" / "skills",
        ),
        session_hint="kiro panel",
    ),
    AgentDef(
        id="copilot",
        name="GitHub Copilot",
        color="white",
        global_dirs=(
            HOME / ".copilot" / "skills",
        ),
        session_hint="VS Code agent",
    ),
    AgentDef(
        id="windsurf",
        name="Windsurf",
        color="bright_blue",
        global_dirs=(
            HOME / ".codeium" / "windsurf" / "skills",
        ),
        session_hint="Cascade skills",
    ),
    AgentDef(
        id="junie",
        name="Junie (JetBrains)",
        color="magenta",
        global_dirs=(
            HOME / ".junie" / "skills",
        ),
        session_hint="JetBrains IDE",
    ),
    AgentDef(
        id="roo",
        name="Roo Code",
        color="bright_red",
        global_dirs=(
            HOME / ".roo" / "skills",
        ),
        session_hint="VS Code extension",
    ),
    AgentDef(
        id="opencode",
        name="OpenCode",
        color="yellow",
        global_dirs=(
            HOME / ".config" / "opencode" / "skills",
        ),
        session_hint="opencode.json",
    ),
    AgentDef(
        id="agents",
        name="Shared (.agents)",
        color="cyan",
        global_dirs=(
            HOME / ".agents" / "skills",
        ),
        session_hint="cross-agent standard",
    ),
    AgentDef(
        id="watch",
        name="watch.conf",
        color="bright_magenta",
        global_dirs=tuple(),
        session_hint="~/.blastgrid/watch.conf",
    ),
]


def get_all_agents() -> list[AgentDef]:
    return list(_AGENTS)


def get_active_agents() -> list[AgentDef]:
    active = []
    for a in _AGENTS:
        if any(d.is_dir() for d in a.global_dirs):
            active.append(a)
    return active


def get_agent(agent_id: str) -> AgentDef | None:
    for a in _AGENTS:
        if a.id == agent_id:
            return a
    return None


def get_all_skill_dirs() -> list[tuple[AgentDef, Path]]:
    pairs = []
    for a in _AGENTS:
        for d in a.global_dirs:
            if d.is_dir():
                pairs.append((a, d))
    return pairs
