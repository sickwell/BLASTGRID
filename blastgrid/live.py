from __future__ import annotations

import collections
import threading
import time

from rich.console import Group
from rich.panel import Panel
from rich.table import Table as RichTable
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import Footer, Static

from .db import UsageTier, CONTEXT_BUDGET

TIMELINE_WINDOW = 600
BUCKET_SIZE = 10
BUCKET_COUNT = TIMELINE_WINDOW // BUCKET_SIZE

BARS = " ▁▂▃▄▅▆▇█"

AGENT_GLYPHS = {
    "antigravity": ("⬡", "bright_cyan"),
    "claude":      ("◈", "bright_magenta"),
    "cursor":      ("▣", "bright_green"),
    "gemini":      ("◇", "bright_yellow"),
    "codex":       ("⬢", "bright_red"),
    "kiro":        ("△", "bright_white"),
    "copilot":     ("⊛", "white"),
    "windsurf":    ("≋", "bright_blue"),
    "junie":       ("◆", "magenta"),
    "roo":         ("⊕", "bright_red"),
    "opencode":    ("◎", "yellow"),
    "agents":      ("●", "cyan"),
    "watch":       ("◉", "bright_magenta"),
}


class HitEvent:
    __slots__ = ("agent", "name", "sid", "ts")

    def __init__(self, agent: str, name: str, sid: str, ts: float):
        self.agent = agent
        self.name = name
        self.sid = sid
        self.ts = ts


class DaemonState:
    def __init__(self):
        self.start_time = time.time()
        self.hits: list[HitEvent] = []
        self.skill_counts: dict[str, int] = collections.defaultdict(int)
        self.skill_names: dict[str, tuple[str, str]] = {}
        self.lock = threading.Lock()
        self.monitoring: int = 0
        self.backend: str = ""
        self.error: str = ""

    def record_hit(self, agent: str, name: str, sid: str):
        with self.lock:
            self.hits.append(HitEvent(agent, name, sid, time.time()))
            self.skill_counts[sid] += 1
            self.skill_names[sid] = (agent, name)

    def timeline_buckets(self) -> list[int]:
        now = time.time()
        cutoff = now - TIMELINE_WINDOW
        buckets = [0] * BUCKET_COUNT
        with self.lock:
            for h in self.hits:
                if h.ts < cutoff:
                    continue
                idx = int((h.ts - cutoff) / BUCKET_SIZE)
                if 0 <= idx < BUCKET_COUNT:
                    buckets[idx] += 1
        return buckets

    def top_skills(self, limit: int = 10) -> list[tuple[str, str, str, int]]:
        with self.lock:
            items = sorted(self.skill_counts.items(), key=lambda x: -x[1])[:limit]
            return [
                (sid, *self.skill_names.get(sid, ("?", sid)), c)
                for sid, c in items
            ]

    def recent_aggregated(self, limit: int = 8) -> list[tuple[str, str, str, int, float]]:
        """Deduplicated: (sid, agent, name, count, last_ts) by recency."""
        with self.lock:
            last_ts: dict[str, float] = {}
            for h in self.hits:
                if h.sid not in last_ts or h.ts > last_ts[h.sid]:
                    last_ts[h.sid] = h.ts
            items = []
            for sid, count in self.skill_counts.items():
                agent, name = self.skill_names.get(sid, ("?", sid))
                ts = last_ts.get(sid, 0)
                items.append((sid, agent, name, count, ts))
            items.sort(key=lambda x: -x[4])
            return items[:limit]

    @property
    def total(self) -> int:
        with self.lock:
            return len(self.hits)

    @property
    def unique(self) -> int:
        with self.lock:
            return len(self.skill_counts)

    @property
    def elapsed(self) -> str:
        d = int(time.time() - self.start_time)
        h, r = divmod(d, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"


def _chart(buckets: list[int], chart_w: int = 56, chart_h: int = 8) -> Panel:
    vals = list(buckets)
    while len(vals) < BUCKET_COUNT:
        vals.insert(0, 0)

    if chart_w < BUCKET_COUNT:
        ratio = BUCKET_COUNT / chart_w
        compressed = []
        for i in range(chart_w):
            s, e = int(i * ratio), int((i + 1) * ratio)
            compressed.append(sum(vals[s:e]))
        vals = compressed
    elif chart_w > BUCKET_COUNT:
        vals = vals[-BUCKET_COUNT:]
        chart_w = BUCKET_COUNT
    else:
        vals = vals[-chart_w:]

    mx = max(vals) if vals else 0
    cw = len(vals)
    out = Text()

    if mx == 0:
        for _ in range(chart_h):
            out.append("     " + "·" * cw + "\n", style="dim")
        out.append(f"     {'─' * cw}\n", style="dim")
        out.append("     Waiting for calls...", style="dim italic")
    else:
        for row in range(chart_h):
            if row == 0:
                out.append(f"{mx:>3} │", style="dim yellow")
            elif row == chart_h // 2:
                out.append(f"{mx // 2:>3} │", style="dim yellow")
            else:
                out.append("    │", style="dim")

            for col, val in enumerate(vals):
                nrm = (val / mx) * chart_h * 8
                thr = (chart_h - 1 - row) * 8

                if nrm >= thr + 8:
                    ch = "█"
                elif nrm > thr:
                    ch = BARS[min(int(nrm - thr), 8)]
                else:
                    ch = " "

                if ch != " ":
                    pct = val / mx
                    if col == cw - 1:
                        sty = "bold bright_white"
                    elif pct > 0.75:
                        sty = "bright_red"
                    elif pct > 0.5:
                        sty = "bright_yellow"
                    elif pct > 0.25:
                        sty = "bright_green"
                    else:
                        sty = "green"
                    out.append(ch, style=sty)
                else:
                    out.append(" ")
            out.append("\n")

        out.append(f"  0 └{'─' * cw}\n", style="dim")

        if cw >= 20:
            arr = list(" " * cw)
            for i, c in enumerate("-10m"):
                if i < cw:
                    arr[i] = c
            mid = cw // 2
            for i, c in enumerate("-5m"):
                p = mid - 1 + i
                if 0 <= p < cw:
                    arr[p] = c
            for i, c in enumerate("now"):
                p = cw - 3 + i
                if 0 <= p < cw:
                    arr[p] = c
            out.append("     " + "".join(arr), style="dim cyan")

    return Panel(
        out,
        title="[bold bright_green]ACTIVITY[/] [dim](10 min)[/]",
        border_style="green",
        padding=(0, 1),
    )


def _leaderboard(top: list[tuple[str, str, str, int]]) -> Panel:
    if not top:
        return Panel(
            Text("  Waiting for calls...\n\n  Skills will appear\n  as agents use them", style="dim"),
            title="[bold bright_cyan]MOST CALLED[/]",
            border_style="cyan",
        )

    t = RichTable(show_header=False, expand=True, padding=(0, 0), box=None)
    t.add_column("#", width=3, style="dim")
    t.add_column("", width=2)
    t.add_column("SKILL", min_width=10, no_wrap=True)
    t.add_column("", min_width=6)
    t.add_column("", width=4, justify="right")

    mx = top[0][3] if top else 1
    for i, (_, agent, name, cnt) in enumerate(top, 1):
        glyph, color = AGENT_GLYPHS.get(agent, ("?", "white"))
        bw = max(1, int((cnt / mx) * 6))

        pct = cnt / mx
        if pct > 0.75:
            bar_color = "bright_red"
        elif pct > 0.5:
            bar_color = "bright_yellow"
        else:
            bar_color = "bright_green"

        t.add_row(
            str(i),
            Text(glyph, style=f"bold {color}"),
            Text(name[:18], style="bold"),
            Text("█" * bw + "░" * (6 - bw), style=bar_color),
            Text(str(cnt), style="bold bright_yellow"),
        )

    return Panel(t, title="[bold bright_cyan]MOST CALLED[/]", border_style="cyan")


def _feed(events: list[tuple[str, str, str, int, float]], start: float) -> Panel:
    """Aggregated feed: one line per skill with call count."""
    if not events:
        return Panel(
            Text("  Monitoring... no calls yet", style="dim"),
            title="[bold yellow]RECENT[/]",
            border_style="yellow",
        )

    out = Text()
    for _, agent, name, cnt, ts in events:
        d = int(ts - start)
        hh, r = divmod(d, 3600)
        mm, ss = divmod(r, 60)
        glyph, color = AGENT_GLYPHS.get(agent, ("?", "white"))

        out.append(f"  {hh:02d}:{mm:02d}:{ss:02d} ", style="dim")
        out.append(f"{glyph} ", style=f"bold {color}")
        out.append(f"{name[:24]:<24s}", style="bold")
        if cnt > 1:
            out.append(f" x{cnt}", style="bright_yellow")
        out.append(f"  [{agent[:8]}]", style=f"dim {color}")
        out.append("\n")

    return Panel(out, title="[bold yellow]RECENT[/]", border_style="yellow")


def _usage_breakdown(tiers: list[UsageTier], budget: int = CONTEXT_BUDGET) -> Panel:
    """Usage tier breakdown with actionable insight."""
    TIER_META = {
        "heavy": ("5+ calls", "bright_green"),
        "used":  ("2-4 calls", "green"),
        "once":  ("1 call", "yellow"),
        "ghost": ("0 calls", "bright_red"),
    }

    t = RichTable(show_header=True, expand=True, padding=(0, 1), box=None)
    t.add_column("TIER", width=7)
    t.add_column("CALLS", width=10)
    t.add_column("SKILLS", width=7, justify="right")
    t.add_column("TOKENS", width=9, justify="right")
    t.add_column("", min_width=16)

    max_count = max((tier.count for tier in tiers), default=1) or 1
    ghost_tokens = 0

    for tier in tiers:
        calls_label, color = TIER_META.get(tier.label, ("?", "white"))
        bw = max(1, int(tier.count / max_count * 16))

        t.add_row(
            Text(tier.label.upper(), style=f"bold {color}"),
            Text(calls_label, style="dim"),
            Text(str(tier.count), style="bold"),
            Text(f"{tier.tokens:,}", style="dim"),
            Text("█" * bw + "░" * (16 - bw), style=color),
        )

        if tier.label == "ghost":
            ghost_tokens = tier.tokens

    parts: list = [t]

    if ghost_tokens > 0 and budget > 0:
        pct = ghost_tokens / budget * 100
        parts.append(Text.from_markup(
            f"\n  [bold bright_yellow]Removing ghosts frees "
            f"{ghost_tokens:,} tokens ({pct:.0f}% of budget)[/]"
            f"  [dim]→ press 3 for HUNT[/]"
        ))

    return Panel(
        Group(*parts),
        title="[bold bright_cyan]SESSION USAGE[/]",
        border_style="bright_cyan",
    )


class DaemonApp(App):
    DEFAULT_CSS = """
    Screen {
        background: #0a0e1a;
    }
    #dh {
        height: 3;
        background: #0c1428;
        border: heavy #00ccff;
        padding: 0 1;
        content-align-vertical: middle;
    }
    #main {
        height: 1fr;
    }
    #tl {
        width: 2fr;
        overflow-y: auto;
    }
    #lb {
        width: 1fr;
        overflow-y: auto;
    }
    #fd {
        height: auto;
        max-height: 14;
    }
    #err {
        height: 3;
        background: #2a0000;
        border: heavy red;
        padding: 0 1;
        display: none;
    }
    """

    BINDINGS = [Binding("q", "quit", "QUIT")]

    def __init__(self, daemon_state: DaemonState):
        super().__init__()
        self.st = daemon_state
        self._tick = False

    def compose(self) -> ComposeResult:
        yield Static(id="dh")
        yield Static(id="err")
        with Horizontal(id="main"):
            yield Static(id="tl")
            yield Static(id="lb")
        yield Static(id="fd")
        yield Footer()

    def on_mount(self):
        self.set_interval(1.0, self._refresh)
        self._refresh()

    def _refresh(self):
        self._tick = not self._tick
        s = self.st

        pulse = "●" if self._tick else "○"
        p_style = "bold bright_green" if self._tick else "dim green"
        hdr = Text()
        hdr.append(f" {pulse} ", style=p_style)
        hdr.append("BLASTGRID DAEMON", style="bold bright_cyan")
        hdr.append("  │  ", style="dim")
        hdr.append(f"T {s.elapsed}", style="bold bright_white")
        hdr.append("  │  ", style="dim")
        hdr.append(f"{s.total} calls", style="bold bright_yellow")
        hdr.append("  │  ", style="dim")
        hdr.append(f"{s.unique} skills", style="bold bright_green")
        hdr.append("  │  ", style="dim")
        hdr.append(f"{s.monitoring} watched", style="dim")
        hdr.append(f"  [{s.backend}]", style="dim cyan")
        self.query_one("#dh").update(hdr)

        err_w = self.query_one("#err")
        if s.error:
            err_w.update(Text(f" Error: {s.error}", style="bold red"))
            err_w.styles.display = "block"
        else:
            err_w.styles.display = "none"

        try:
            tw = self.query_one("#tl").content_size.width - 10
        except Exception:
            tw = 50
        self.query_one("#tl").update(_chart(s.timeline_buckets(), max(20, tw)))
        self.query_one("#lb").update(_leaderboard(s.top_skills(14)))
        self.query_one("#fd").update(
            _feed(s.recent_aggregated(8), s.start_time)
        )
