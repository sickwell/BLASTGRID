from __future__ import annotations

import hashlib
from pathlib import Path

from .agents import AgentDef, get_all_skill_dirs
from .db import WATCH_AGENT_ID, WATCH_CONF, SkillRecord


def _watch_entry_name(abs_file: Path) -> str:
    h = hashlib.sha1(str(abs_file.resolve()).encode()).hexdigest()[:14]
    return f"w{h}"


def _parse_watch_conf_lines() -> list[Path]:
    if not WATCH_CONF.is_file():
        return []
    raw: list[Path] = []
    for line in WATCH_CONF.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        p = Path(line).expanduser()
        try:
            p = p.resolve()
        except OSError:
            continue
        raw.append(p)
    return raw


def _files_under_watch_root(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if ".git" in p.parts:
            continue
        out.append(p)
    return sorted(out)


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


def scan_watch_extra() -> list[SkillRecord]:
    entries: list[SkillRecord] = []
    seen_paths: set[str] = set()
    for raw in _parse_watch_conf_lines():
        paths: list[Path]
        if raw.is_file():
            paths = [raw]
        elif raw.is_dir():
            paths = _files_under_watch_root(raw)
        else:
            continue

        for fp in paths:
            key = str(fp.resolve())
            if key in seen_paths:
                continue
            seen_paths.add(key)
            name = _watch_entry_name(fp)
            try:
                text = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            sz = len(text.encode("utf-8")) if text else 0
            entries.append(
                SkillRecord(
                    name=name,
                    path=key,
                    agent=WATCH_AGENT_ID,
                    size_bytes=sz,
                    token_count=estimate_tokens(text) if text else 1,
                    description=key[-80:] if len(key) > 80 else key,
                    content_hash=content_hash(text) if text else None,
                )
            )
    return entries


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
    all_skills.extend(scan_watch_extra())
    return all_skills


def scan_single_agent(agent: AgentDef) -> list[SkillRecord]:
    results: list[SkillRecord] = []
    for d in agent.global_dirs:
        results.extend(_scan_dir(agent, d))
    return results


