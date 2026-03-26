from __future__ import annotations

import hashlib
from pathlib import Path

from .agents import AgentDef, get_all_skill_dirs
from .db import SkillRecord


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def extract_desc(content: str, limit: int = 100) -> str:
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", "---", "```", ">")):
            continue
        if len(line) > 15:
            return line[:limit]
    return ""


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _scan_dir(agent: AgentDef, skills_dir: Path) -> list[SkillRecord]:
    results: list[SkillRecord] = []
    if not skills_dir.is_dir():
        return results

    for item in sorted(skills_dir.iterdir()):
        if not item.is_dir():
            continue
        skill_md = item / "SKILL.md"
        if not skill_md.exists():
            continue
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        results.append(SkillRecord(
            name=item.name,
            path=str(item),
            agent=agent.id,
            size_bytes=len(text.encode("utf-8")),
            token_count=estimate_tokens(text),
            description=extract_desc(text),
            content_hash=content_hash(text),
        ))
    return results


def scan_all_agents() -> list[SkillRecord]:
    all_skills: list[SkillRecord] = []
    for agent, directory in get_all_skill_dirs():
        all_skills.extend(_scan_dir(agent, directory))
    return all_skills


def scan_single_agent(agent: AgentDef) -> list[SkillRecord]:
    results: list[SkillRecord] = []
    for d in agent.global_dirs:
        results.extend(_scan_dir(agent, d))
    return results


