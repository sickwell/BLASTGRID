from __future__ import annotations

import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .db import WATCH_AGENT_ID


def cli_scan():
    from .agents import get_active_agents
    from .db import SkillDB
    from .scanner import scan_all_agents

    db = SkillDB()
    skills = scan_all_agents()
    db.upsert_skills(skills)

    agents = get_active_agents()
    print(f"⚡ Scanned {len(skills)} skills across {len(agents)} agents:")
    agent_counts: dict[str, int] = {}
    for s in skills:
        agent_counts[s.agent] = agent_counts.get(s.agent, 0) + 1
    for a, c in sorted(agent_counts.items(), key=lambda x: -x[1]):
        print(f"  {a:15s} {c:5d} skills")
    db.close()


def cli_stats():
    from .db import CONTEXT_BUDGET, SkillDB

    db = SkillDB()
    s = db.get_stats()
    breakdown = db.get_agent_breakdown()
    dupes = db.get_duplicates()
    top = db.get_top_used(5)
    db.close()

    print("◆ BLASTGRID STATS ◆")
    print(f"  Skills:  {s.total}")
    print(f"  Active:  {s.used}")
    print(f"  Ghosts:  {s.unused}")
    print(f"  Context: {s.budget_pct:.0f}% ({s.total_tokens:,} / {CONTEXT_BUDGET:,} tkn)")
    print(f"  Tagged:  keep={s.tagged_keep}  remove={s.tagged_remove}")
    print(f"  Dupes:   {len(dupes)} names shared across agents")

    if breakdown:
        print("\n  Agents:")
        for a in breakdown:
            print(f"    {a.agent:15s}  {a.count:5d} skills  {a.tokens:>10,} tkn")

    if top:
        print("\n  Top used:")
        for i, sk in enumerate(top, 1):
            print(f"    {i}. [{sk.agent}] {sk.name} ({sk.use_count} uses)")


def cli_tag(args: list[str]):
    from .db import SkillDB

    if len(args) < 2:
        print("Usage: blastgrid tag <keep|remove|clear> <agent:skill | file.txt>")
        print("  If no agent: prefix, tags all agents matching that skill name.")
        return
    action, target = args[0], args[1]
    tag_val = None if action == "clear" else action

    db = SkillDB()

    if os.path.isfile(target):
        with open(target, encoding="utf-8") as f:
            entries = [ln.strip() for ln in f if ln.strip()]
    else:
        entries = [target]

    for entry in entries:
        if ":" in entry:
            db.tag_skill(entry, tag_val)
            print(f"  {entry} → {tag_val or 'cleared'}")
        else:
            skills = db.get_all(search=entry)
            matched = [s for s in skills if s.name == entry]
            for s in matched:
                sid = db.skill_id(s.agent, s.name)
                db.tag_skill(sid, tag_val)
                print(f"  {sid} → {tag_val or 'cleared'}")
            if not matched:
                print(f"  {entry} — not found")
    db.close()


def cli_agents():
    from .agents import get_all_agents

    print("◆ BLASTGRID AGENTS ◆\n")
    for a in get_all_agents():
        found = any(d.is_dir() for d in a.global_dirs)
        status = "✓ ACTIVE" if found else "✗ not found"
        print(f"  {a.id:15s}  {a.name:16s}  {status}")
        for d in a.global_dirs:
            mark = "  →" if d.is_dir() else "   "
            print(f"    {mark} {d}")
    print()


def _get_skill_regex_patterns():
    return [
        (re.compile(r"\.gemini/antigravity/skills/([^/]+)/SKILL\.md"), "antigravity"),
        (re.compile(r"\.claude/skills/([^/]+)/SKILL\.md"), "claude"),
        (re.compile(r"\.cursor/skills[^/]*/([^/]+)/SKILL\.md"), "cursor"),
        (re.compile(r"\.gemini/skills/([^/]+)/SKILL\.md"), "gemini"),
        (re.compile(r"\.codex/skills/([^/]+)/SKILL\.md"), "codex"),
        (re.compile(r"\.kiro/skills/([^/]+)/SKILL\.md"), "kiro"),
        (re.compile(r"\.copilot/skills/([^/]+)/SKILL\.md"), "copilot"),
        (re.compile(r"\.codeium/windsurf/skills/([^/]+)/SKILL\.md"), "windsurf"),
        (re.compile(r"\.junie/skills/([^/]+)/SKILL\.md"), "junie"),
        (re.compile(r"\.roo/skills/([^/]+)/SKILL\.md"), "roo"),
        (re.compile(r"\.config/opencode/skills/([^/]+)/SKILL\.md"), "opencode"),
        (re.compile(r"\.agents/skills/([^/]+)/SKILL\.md"), "agents"),
    ]


def _build_skill_file_map(db) -> dict[str, tuple[str, str, str]]:
    result: dict[str, tuple[str, str, str]] = {}
    skills = db.get_all()
    for s in skills:
        sid = db.skill_id(s.agent, s.name)
        if s.agent == WATCH_AGENT_ID:
            fp = str(Path(s.path))
            if Path(fp).is_file():
                result[fp] = (s.agent, s.name, sid)
            continue
        md_path = str(Path(s.path) / "SKILL.md")
        result[md_path] = (s.agent, s.name, sid)
    return result


def _watch_paths_for_fs_usage(skill_map: dict[str, tuple[str, str, str]]):
    return sorted(
        [(p, skill_map[p]) for p in skill_map if skill_map[p][0] == WATCH_AGENT_ID],
        key=lambda t: len(t[0]),
        reverse=True,
    )


def _daemon_inotify_wait(
    proc: subprocess.Popen,
    q: queue.Queue,
):
    try:
        out = proc.stdout
        if not out:
            return
        for line in out:
            q.put(line)
    except Exception:
        pass


def _daemon_inotifywait(
    db,
    patterns,
    dirs: list[str],
    skill_map: dict[str, tuple[str, str, str]],
):
    watch_files = sorted(
        p for p, (a, _, _) in skill_map.items() if a == WATCH_AGENT_ID
    )
    q: queue.Queue[str | None] = queue.Queue()
    procs: list[subprocess.Popen] = []

    if dirs:
        procs.append(
            subprocess.Popen(
                [
                    "inotifywait", "-m", "-r",
                    "-e", "access",
                    "--include", r"SKILL\.md",
                    "--format", "%w%f",
                ] + dirs,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )
    if watch_files:
        procs.append(
            subprocess.Popen(
                [
                    "inotifywait", "-m",
                    "-e", "access",
                    "--format", "%w%f",
                ] + watch_files,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        )

    for proc in procs:
        threading.Thread(
            target=_daemon_inotify_wait,
            args=(proc, q),
            daemon=True,
        ).start()

    if not procs:
        return

    if not _live_mode:
        print(
            f"  Backend: inotifywait (agent dirs: {len(dirs)}, "
            f"extra files: {len(watch_files)})\n"
        )

    last_status = time.time()
    while True:
        try:
            line = q.get(timeout=30.0)
        except queue.Empty:
            line = None
        if line is None:
            if not _live_mode:
                now = time.time()
                if now - last_status >= 30:
                    last_status = now
                    elapsed = _fmt_elapsed(_session_start)
                    print(
                        f"  [{elapsed}] ● session: {len(_session_unique)} unique, "
                        f"{_session_hits} hits"
                    )
            continue

        last_status = time.time()
        row = line.strip()
        if not row:
            continue
        matched = False
        for regex, agent_id in patterns:
            m = regex.search(row)
            if m:
                name = m.group(1)
                sid = f"{agent_id}:{name}"
                _log_hit(db, agent_id, name, sid, "daemon")
                matched = True
                break
        if not matched:
            npath = os.path.normpath(row)
            for fp, (agent_id, name, sid) in skill_map.items():
                if agent_id != WATCH_AGENT_ID:
                    continue
                try:
                    if os.path.normpath(fp) == npath:
                        _log_hit(db, agent_id, name, sid, "daemon")
                        matched = True
                        break
                except OSError:
                    continue


def _fmt_elapsed(start: float) -> str:
    d = int(time.time() - start)
    h, r = divmod(d, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


_session_start: float = 0.0
_session_hits: int = 0
_session_unique: set[str] = set()
_daemon_state = None
_live_mode = False


def _log_to_file(agent_id: str, name: str, sid: str, source: str):
    """Append a JSON-lines entry to ~/.blastgrid/usage.log with auto-rotation."""
    from .db import LOG_PATH, LOG_MAX_BYTES
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            rotated = LOG_PATH.with_suffix(".log.1")
            if rotated.exists():
                rotated.unlink()
            LOG_PATH.rename(rotated)
        entry = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "agent": agent_id,
            "skill": name,
            "sid": sid,
            "source": source,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _log_hit(db, agent_id: str, name: str, sid: str, source: str):
    global _session_hits
    try:
        db.log_usage(sid, source)
        _session_hits += 1
        is_new = sid not in _session_unique
        _session_unique.add(sid)

        _log_to_file(agent_id, name, sid, source)

        if _daemon_state is not None:
            _daemon_state.record_hit(agent_id, name, sid)

        if not _live_mode:
            elapsed = _fmt_elapsed(_session_start)
            tag = "NEW" if is_new else f"×{_session_hits}"
            print(f"  [{elapsed}] ⚡ {agent_id}:{name}  ({tag})")
    except Exception:
        pass

def _daemon_fs_usage(
    db,
    patterns,
    skill_map: dict[str, tuple[str, str, str]],
):
    watch_pairs = _watch_paths_for_fs_usage(skill_map)
    if not _live_mode:
        print("  Backend: fs_usage (precise, real-time)\n")
    proc = subprocess.Popen(
        ["fs_usage", "-f", "pathname", "-e", "fs_usage"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    last_status = time.time()
    for line in proc.stdout:
        matched = False
        if "SKILL" in line:
            for regex, agent_id in patterns:
                m = regex.search(line)
                if m:
                    name = m.group(1)
                    sid = f"{agent_id}:{name}"
                    _log_hit(db, agent_id, name, sid, "daemon")
                    matched = True
                    break
        if not matched and watch_pairs:
            for fp, (agent_id, name, sid) in watch_pairs:
                if fp in line:
                    _log_hit(db, agent_id, name, sid, "daemon")
                    matched = True
                    break
        if not matched and not _live_mode:
            now = time.time()
            if now - last_status >= 30:
                last_status = now
                elapsed = _fmt_elapsed(_session_start)
                print(
                    f"  [{elapsed}] ● session: {len(_session_unique)} unique, "
                    f"{_session_hits} hits"
                )


def _test_atime_priming(sample_path: str) -> bool:
    """Verify atime priming: set atime before mtime, read, check atime updated."""
    try:
        st = os.stat(sample_path)
        old_atime = st.st_atime
        os.utime(sample_path, (st.st_mtime - 86400, st.st_mtime))
        time.sleep(0.05)
        before = os.stat(sample_path).st_atime_ns
        with open(sample_path, "r") as f:
            f.read(64)
        after = os.stat(sample_path).st_atime_ns
        os.utime(sample_path, (old_atime, st.st_mtime))
        return after > before
    except Exception:
        return False


def _daemon_python(db, skill_map: dict[str, tuple[str, str, str]]):
    """Pure-Python atime-polling daemon. Works on macOS APFS + Linux.

    Trick: APFS (and Linux relatime) only update atime when atime < mtime.
    By resetting atime to the past, any subsequent read triggers an atime update.
    After detecting a read, we re-prime the file for the next read.
    """
    sample = next(iter(skill_map), None)
    if sample and not _test_atime_priming(sample):
        msg = "atime priming not supported (volume mounted with noatime?)"
        if _daemon_state is not None:
            _daemon_state.error = msg
        if not _live_mode:
            print(f"  ✗ {msg}")
            if platform.system() == "Linux":
                print("  Fix: sudo apt install inotify-tools && blastgrid daemon")
                print("  Or:  sudo mount -o remount,relatime /")
            else:
                print("  macOS: try sudo blastgrid daemon (uses fs_usage)")
        return

    primed = 0

    if not _live_mode:
        print(f"  Backend: Python atime polling (interval: 1s)")
        print(f"  Priming {len(skill_map)} file(s)...")

    for path in skill_map:
        try:
            st = os.stat(path)
            os.utime(path, (st.st_mtime - 86400, st.st_mtime))
            primed += 1
        except OSError:
            pass

    if not _live_mode:
        print(f"  Primed: {primed} files, settling...")

    time.sleep(3)

    atimes: dict[str, int] = {}
    for path in skill_map:
        try:
            st = os.stat(path)
            os.utime(path, (st.st_mtime - 86400, st.st_mtime))
            atimes[path] = os.stat(path).st_atime_ns
        except OSError:
            pass

    if not _live_mode:
        print(f"  Session started — polling every 1s\n")

    seen_this_cycle: set[str] = set()
    last_status = time.time()

    while True:
        time.sleep(1)
        seen_this_cycle.clear()

        for path, (agent_id, name, sid) in skill_map.items():
            try:
                cur = os.stat(path).st_atime_ns
            except OSError:
                continue
            prev = atimes.get(path, 0)
            if cur > prev:
                atimes[path] = cur
                if sid not in seen_this_cycle:
                    seen_this_cycle.add(sid)
                    _log_hit(db, agent_id, name, sid, "daemon")
                    # Re-prime: set atime before mtime so next read is caught
                    try:
                        st = os.stat(path)
                        os.utime(path, (st.st_mtime - 86400, st.st_mtime))
                        atimes[path] = os.stat(path).st_atime_ns
                    except OSError:
                        pass

        if not _live_mode:
            now = time.time()
            if now - last_status >= 30:
                last_status = now
                elapsed = _fmt_elapsed(_session_start)
                print(
                    f"  [{elapsed}] ● session: {len(_session_unique)} unique skills, "
                    f"{_session_hits} total hits, monitoring {len(atimes)} files"
                )


def cli_daemon():
    global _session_start, _session_hits, _session_unique, _daemon_state, _live_mode

    from .agents import get_all_skill_dirs
    from .db import SkillDB

    headless = "--headless" in sys.argv

    db = SkillDB()

    from .scanner import scan_all_agents
    skills = scan_all_agents()
    if skills:
        db.upsert_skills(skills)

    db.reset_session()
    _session_start = time.time()
    _session_hits = 0
    _session_unique = set()

    patterns = _get_skill_regex_patterns()
    skill_map = _build_skill_file_map(db)
    watch_dirs = [str(d) for _, d in get_all_skill_dirs()]

    is_mac = platform.system() == "Darwin"
    is_linux = platform.system() == "Linux"
    is_root = os.geteuid() == 0 if hasattr(os, "geteuid") else False

    # Determine backend
    if is_mac and is_root:
        backend_name = "fs_usage"
        def _run_backend():
            _daemon_fs_usage(db, patterns, skill_map)
    elif is_linux and shutil.which("inotifywait"):
        backend_name = "inotifywait"
        def _run_backend():
            _daemon_inotifywait(db, patterns, watch_dirs, skill_map)
    else:
        backend_name = "atime-poll"
        def _run_backend():
            _daemon_python(db, skill_map)

    if not headless:
        from .live import DaemonState, DaemonApp

        _daemon_state = DaemonState()
        _daemon_state.monitoring = len(skill_map)
        _daemon_state.backend = backend_name
        _live_mode = True

        def _backend_thread():
            try:
                _run_backend()
            except Exception as e:
                if _daemon_state is not None:
                    _daemon_state.error = str(e)

        t = threading.Thread(target=_backend_thread, daemon=True)
        t.start()

        try:
            DaemonApp(_daemon_state).run()
        finally:
            _live_mode = False
            _daemon_state = None
            db.close()
    else:
        ts_start = datetime.now().strftime("%H:%M:%S")
        print("◆ BLASTGRID DAEMON — skill usage tracker (Ctrl+C to stop)")
        print(f"  Platform: {platform.system()}")
        print(f"  Root: {'yes' if is_root else 'no'}")
        print(f"  Skills: {len(skill_map)} across {len(watch_dirs)} dirs")
        print(f"  Session: NEW (counters reset at {ts_start})")
        print(f"  Backend: {backend_name}")

        try:
            _run_backend()
        except KeyboardInterrupt:
            print("\n◆ Daemon stopped")
        except Exception as e:
            print(f"\n  ✗ Daemon error: {e}")
        finally:
            db.close()


def cli_top(args: list[str]):
    from rich.console import Console
    from rich.table import Table
    from .db import SkillDB, CONTEXT_BUDGET

    limit = 50
    if args and args[0].isdigit():
        limit = int(args[0])

    db = SkillDB()
    skills = db.get_all(sort_by="use_count", desc=True)
    tiers = db.get_usage_tiers()
    stats = db.get_stats()
    db.close()

    console = Console()

    # Usage tier summary
    console.print("\n[bold bright_cyan]◆ USAGE TIERS[/]")
    tier_table = Table(show_header=True, padding=(0, 1))
    tier_table.add_column("TIER", width=7)
    tier_table.add_column("CALLS", width=10)
    tier_table.add_column("SKILLS", width=8, justify="right")
    tier_table.add_column("TOKENS", width=10, justify="right")
    for t in tiers:
        meta = {"heavy": ("5+", "bright_green"), "used": ("2-4", "green"),
                "once": ("1", "yellow"), "ghost": ("0", "bright_red")}
        calls, color = meta.get(t.label, ("?", "white"))
        tier_table.add_row(
            f"[{color}]{t.label.upper()}[/]", calls, str(t.count), f"{t.tokens:,}")

    ghost_tkn = sum(t.tokens for t in tiers if t.label == "ghost")
    console.print(tier_table)
    if ghost_tkn and CONTEXT_BUDGET:
        pct = ghost_tkn / CONTEXT_BUDGET * 100
        console.print(
            f"  [bold bright_yellow]Removing ghosts frees {ghost_tkn:,} tokens "
            f"({pct:.0f}% of budget)[/]\n")

    # Full leaderboard
    console.print(f"[bold bright_cyan]◆ TOP {limit} SKILLS BY USAGE[/]")
    t = Table(show_header=True, padding=(0, 1))
    t.add_column("#", width=4, justify="right")
    t.add_column("AGENT", width=13)
    t.add_column("SKILL", min_width=24, no_wrap=True)
    t.add_column("USES", width=5, justify="right")
    t.add_column("TOKENS", width=8, justify="right")
    t.add_column("TIER", width=5)
    t.add_column("TAG", width=4)
    t.add_column("LAST USED", width=20)

    for i, s in enumerate(skills[:limit], 1):
        tier_meta = {"BOSS": "red", "RARE": "yellow", "COMM": "cyan", "LITE": "green"}
        tier_s = _tier_label(s.token_count)
        tier_c = tier_meta.get(tier_s, "white")
        use_c = "bright_green" if s.use_count > 0 else "dim red"
        tag_s = s.tag.upper() if s.tag else "—"
        t.add_row(
            str(i), s.agent, s.name,
            f"[{use_c}]{s.use_count}[/]",
            f"{s.token_count:,}",
            f"[{tier_c}]{tier_s}[/]",
            tag_s,
            (s.last_used[:19] if s.last_used else "[dim]—[/]"),
        )

    console.print(t)
    console.print(f"\n  Total: {stats.total} skills, {stats.total_tokens:,} tokens\n")


def _tier_label(tokens: int) -> str:
    if tokens > 5000: return "BOSS"
    if tokens > 2000: return "RARE"
    if tokens > 800: return "COMM"
    return "LITE"


def cli_log(args: list[str]):
    from rich.console import Console
    from rich.table import Table
    from .db import LOG_PATH

    console = Console()
    limit = 50
    if args and args[0].isdigit():
        limit = int(args[0])

    if not LOG_PATH.exists():
        console.print("[dim]No usage log yet. Run the daemon first.[/]")
        return

    lines = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    recent = lines[-limit:]

    console.print(f"\n[bold bright_cyan]◆ USAGE LOG[/] [dim](last {len(recent)} of {len(lines)} events)[/]")
    console.print(f"  [dim]{LOG_PATH}[/]\n")

    t = Table(show_header=True, padding=(0, 1))
    t.add_column("TIMESTAMP", width=20)
    t.add_column("AGENT", width=14)
    t.add_column("SKILL", min_width=20)
    t.add_column("SOURCE", width=8)

    for line in recent:
        try:
            entry = json.loads(line)
            t.add_row(
                entry.get("ts", "?"),
                entry.get("agent", "?"),
                entry.get("skill", "?"),
                entry.get("source", "?"),
            )
        except json.JSONDecodeError:
            continue

    console.print(t)
    console.print(f"\n  File: {LOG_PATH}")
    size = LOG_PATH.stat().st_size
    if size > 1024 * 1024:
        console.print(f"  Size: {size / 1024 / 1024:.1f} MB")
    else:
        console.print(f"  Size: {size / 1024:.0f} KB")
    console.print()


def cli_restore(args: list[str]):
    from .agents import get_agent
    from .db import VAULT, GRAVEYARD

    source_dir = VAULT
    source_label = "VAULT"
    target = None

    remaining = list(args)
    if "--graveyard" in remaining:
        remaining.remove("--graveyard")
        source_dir = GRAVEYARD
        source_label = "GRAVEYARD"

    if remaining:
        target = remaining[0]

    if not target:
        for loc, label in [(VAULT, "VAULT"), (GRAVEYARD, "GRAVEYARD")]:
            if loc.is_dir():
                items = sorted(loc.iterdir())
                if items:
                    print(f"\n  {label} ({loc}):")
                    for item in items:
                        size = sum(f.stat().st_size for f in item.rglob("*") if f.is_file()) if item.is_dir() else 0
                        print(f"    {item.name}  ({size // 1024}K)")
                else:
                    print(f"\n  {label}: empty")
            else:
                print(f"\n  {label}: not created yet")
        print("\n  Usage: blastgrid restore <agent__skillname> [--graveyard]")
        return

    src = source_dir / target
    if not src.is_dir():
        print(f"  ✗ Not found in {source_label}: {target}")
        print(f"    Run 'blastgrid restore' to list available items")
        return

    parts = target.split("__", 1)
    if len(parts) != 2:
        print(f"  ✗ Invalid format: {target}")
        print(f"    Expected: agent__skillname (e.g. antigravity__python-pro)")
        return

    agent_id, skill_name = parts
    # Strip dedup suffix (~~<timestamp>) added by vault when name already existed
    if "~~" in skill_name:
        skill_name = skill_name.rsplit("~~", 1)[0]
    if agent_id == WATCH_AGENT_ID:
        from .vault_ops import restore_watch_vault_folder
        if restore_watch_vault_folder(src):
            print(f"  ✓ Restored watch.conf file to its original path")
            print(f"    Run 'blastgrid scan' to update the database")
        else:
            print(f"  ✗ Could not restore watch vault item (missing .blastgrid-origin?)")
        return

    agent_def = get_agent(agent_id)
    if not agent_def or not agent_def.global_dirs:
        print(f"  ✗ Unknown agent: {agent_id}")
        return

    dest = agent_def.global_dirs[0] / skill_name
    if dest.exists():
        print(f"  ✗ Skill already exists at: {dest}")
        print(f"    Remove it first or rename the existing one")
        return

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))
    print(f"  ✓ Restored: {skill_name} → {dest}")
    print(f"    Run 'blastgrid scan' to update the database")


def main():
    args = sys.argv[1:]

    if not args:
        from .app import BlastGridApp
        BlastGridApp().run()
        return

    cmd = args[0]
    if cmd == "scan":
        cli_scan()
    elif cmd == "stats":
        cli_stats()
    elif cmd == "tag":
        cli_tag(args[1:])
    elif cmd == "agents":
        cli_agents()
    elif cmd == "daemon":
        cli_daemon()
    elif cmd == "top":
        cli_top(args[1:])
    elif cmd == "log":
        cli_log(args[1:])
    elif cmd == "restore":
        cli_restore(args[1:])
    elif cmd in ("-h", "--help", "help"):
        print(
            "BLASTGRID — Multi-agent skill manager\n\n"
            "Usage:\n"
            "  blastgrid              Launch TUI\n"
            "  blastgrid daemon       Live monitoring dashboard\n"
            "  blastgrid daemon --headless\n"
            "  blastgrid scan         Scan all agents\n"
            "  blastgrid stats        Quick stats\n"
            "  blastgrid top [N]      Top N skills by usage\n"
            "  blastgrid log [N]      Last N usage log entries\n"
            "  blastgrid agents       Show agents & paths\n"
            "  blastgrid tag <keep|remove|clear> <agent:name>\n"
            "  blastgrid restore      List vault/graveyard\n"
            "  blastgrid restore <agent__name> [--graveyard]\n"
            "  ~/.blastgrid/watch.conf — optional extra paths to track\n\n"
            "TUI keys:\n"
            "  1/2/3/4  DASHBOARD / ARMORY / HUNT / SECURED\n"
            "  TAB      Cycle agent filter\n"
            "  ENTER    Skill detail (vault/delete/keep/secure)\n"
            "  A        AUTOPWN — mass vault by threshold\n"
            "  S        Toggle SECURED (immune to autopwn)\n"
            "  Z        Restore all from vault\n"
            "  K/R/C    Tag keep / remove / clear\n"
            "  X/U/P    Mark / unmark / purge\n"
            "  B        Vault selected skill\n"
            "  F5       Re-scan    Q  Quit\n"
        )
    else:
        print(f"Unknown command: {cmd}. Try: blastgrid help")
        sys.exit(1)


if __name__ == "__main__":
    main()
