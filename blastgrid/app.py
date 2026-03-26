from __future__ import annotations

import os
import platform
import shutil
import threading
import time
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table as RichTable
from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, ScrollableContainer
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, DataTable, Footer, Input, Static

from .agents import get_active_agents, get_all_skill_dirs, AgentDef
from .db import CONTEXT_BUDGET, GRAVEYARD, VAULT, GridStats, SkillDB, SkillRecord, AgentStats, UsageTier
from .live import DaemonState, _chart, _leaderboard, _feed, _usage_breakdown
from .scanner import scan_all_agents


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
}


def _bar(pct: float, width: int = 30) -> str:
    filled = int(pct / 100 * width)
    empty = width - filled
    c = "green" if pct < 50 else ("yellow" if pct < 80 else "red")
    return f"[{c}]{'█' * filled}[/{c}][dim]{'░' * empty}[/dim]"


def _agent_badge(agent_id: str) -> Text:
    glyph, color = AGENT_GLYPHS.get(agent_id, ("?", "white"))
    return Text(f"{glyph} {agent_id[:8]}", style=f"bold {color}")


def _tier(tokens: int) -> Text:
    if tokens > 5000:
        return Text("BOSS", style="bold red")
    if tokens > 2000:
        return Text("RARE", style="bold yellow")
    if tokens > 800:
        return Text("COMM", style="cyan")
    return Text("LITE", style="green")


def _tier_str(tokens: int) -> str:
    if tokens > 5000:
        return "BOSS"
    if tokens > 2000:
        return "RARE"
    if tokens > 800:
        return "COMM"
    return "LITE"


def _tag_text(tag: str | None) -> Text:
    if tag == "keep":
        return Text("KEEP", style="bold green")
    if tag == "remove":
        return Text("☠ REM", style="bold red")
    if tag == "secure":
        return Text("🛡 SEC", style="bold bright_cyan")
    return Text("—", style="dim")


class ConfirmModal(ModalScreen):

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
    }
    #confirm-box {
        width: 64;
        height: auto;
        max-height: 16;
        background: #0c1428;
        border: double #ff4444;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("y", "confirm_yes", "YES", priority=True),
        Binding("enter", "confirm_yes", "YES", priority=True, show=False),
        Binding("n", "confirm_no", "NO", priority=True),
        Binding("escape", "confirm_no", "NO", priority=True, show=False),
    ]

    def __init__(self, title: str, body: str, action_id: str) -> None:
        super().__init__()
        self._title = title
        self._body = body
        self._action_id = action_id

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(id="confirm-body")

    def on_mount(self) -> None:
        out = Text()
        out.append(f"\n  {self._title}\n\n", style="bold bright_yellow")
        out.append(f"  {self._body}\n\n", style="")
        out.append("  [Y] Confirm   [N/ESC] Cancel", style="dim")
        self.query_one("#confirm-body").update(out)

    def action_confirm_yes(self) -> None:
        self.dismiss(self._action_id)

    def action_confirm_no(self) -> None:
        self.dismiss(None)


class AutopwnModal(ModalScreen):
    DEFAULT_CSS = """
    AutopwnModal {
        align: center middle;
    }
    #autopwn-box {
        width: 68;
        height: auto;
        max-height: 22;
        background: #0c1428;
        border: double #ff4444;
        padding: 1 2;
    }
    #autopwn-header {
        margin-bottom: 1;
    }
    #autopwn-input {
        height: 3;
        margin: 0 2;
        background: #0c1222;
        color: #ffcc00;
        border: heavy #ff4444;
    }
    #autopwn-input:focus {
        border: heavy #ffcc00;
    }
    #autopwn-preview {
        margin-top: 1;
    }
    #autopwn-hint {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "CANCEL", priority=True),
    ]

    def __init__(self, db: SkillDB, agent_filter: str = "") -> None:
        super().__init__()
        self._db = db
        self._agent_filter = agent_filter

    def compose(self) -> ComposeResult:
        with Container(id="autopwn-box"):
            yield Static(id="autopwn-header")
            yield Input(
                placeholder="  Threshold (default: 1 = unused only)",
                id="autopwn-input",
                type="integer",
            )
            yield Static(id="autopwn-preview")
            yield Static(id="autopwn-hint")

    def on_mount(self) -> None:
        hdr = Text()
        hdr.append("\n  ⚡ AUTOPWN — MASS VAULT\n", style="bold bright_red")
        af = self._agent_filter or "ALL AGENTS"
        hdr.append(f"  Filter: {af}\n", style="dim")
        hdr.append(
            "  Vault all skills with USES < threshold\n"
            "  (secured 🛡 and kept ✦ skills are protected)\n",
            style="",
        )
        self.query_one("#autopwn-header").update(hdr)
        self._update_preview(1)
        self.query_one("#autopwn-hint").update(
            Text.from_markup("  [dim]ENTER=confirm  ESC=cancel[/]")
        )
        self.query_one("#autopwn-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "autopwn-input":
            try:
                val = int(event.value) if event.value.strip() else 1
            except ValueError:
                val = 1
            self._update_preview(max(1, val))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "autopwn-input":
            try:
                val = int(event.value) if event.value.strip() else 1
            except ValueError:
                val = 1
            threshold = max(1, val)
            candidates = self._db.get_autopwn_candidates(
                threshold, self._agent_filter
            )
            if not candidates:
                self.dismiss(None)
                return
            self.dismiss(("autopwn", threshold))

    def _update_preview(self, threshold: int) -> None:
        candidates = self._db.get_autopwn_candidates(
            threshold, self._agent_filter
        )
        total_tkn = sum(c.token_count for c in candidates)

        preview = Text()
        if candidates:
            preview.append(
                f"  ☠ {len(candidates)} skills with < {threshold} uses\n",
                style="bold bright_red",
            )
            preview.append(
                f"  Freeing {total_tkn:,} tokens\n\n",
                style="bold bright_yellow",
            )
            shown = candidates[:8]
            for c in shown:
                preview.append(f"    ☠ ", style="red")
                preview.append(f"{c.name}", style="")
                preview.append(
                    f"  ({c.use_count} uses, {c.token_count:,} tkn)\n",
                    style="dim",
                )
            if len(candidates) > 8:
                preview.append(
                    f"    ... and {len(candidates) - 8} more\n", style="dim"
                )
        else:
            preview.append(
                "  ✓ No skills match this threshold\n",
                style="bold bright_green",
            )
        self.query_one("#autopwn-preview").update(preview)

    def action_cancel(self) -> None:
        self.dismiss(None)


class SkillDetail(ModalScreen):

    DEFAULT_CSS = """
    SkillDetail {
        align: center middle;
    }
    #detail-box {
        width: 74;
        height: auto;
        max-height: 26;
        background: #0c1428;
        border: double #00ccff;
        padding: 1 2;
    }
    #detail-keys {
        margin-top: 1;
        text-align: center;
    }
    """

    BINDINGS = [
        Binding("b", "do_backup", "VAULT", priority=True),
        Binding("d", "do_delete", "DELETE", priority=True),
        Binding("k", "do_keep", "KEEP", priority=True),
        Binding("s", "do_secure", "SECURE", priority=True),
        Binding("escape", "dismiss_detail", "CLOSE", priority=True),
    ]

    def __init__(self, skill: SkillRecord) -> None:
        super().__init__()
        self.skill = skill

    def compose(self) -> ComposeResult:
        with Container(id="detail-box"):
            yield Static(id="detail-body")
            yield Static(id="detail-keys")

    def on_mount(self) -> None:
        self.query_one("#detail-body").update(self._build())
        sec_label = "UNSECURE" if self.skill.tag == "secure" else "SECURE"
        self.query_one("#detail-keys").update(Text.from_markup(
            f"\n[bold cyan]B[/]=VAULT  [bold red]D[/]=GRAVEYARD  "
            f"[bold green]K[/]=KEEP  [bold bright_cyan]S[/]={sec_label}  [dim]ESC=CLOSE[/]"
        ))

    def _build(self) -> Group:
        s = self.skill
        glyph, color = AGENT_GLYPHS.get(s.agent, ("?", "white"))

        header = Text()
        header.append(f"\n  {glyph} ", style=f"bold {color}")
        header.append(s.name, style="bold bright_white underline")
        header.append(f"   [{s.agent}]\n", style=f"dim {color}")

        info = RichTable(show_header=False, box=None, padding=(0, 1))
        info.add_column("", width=10, style="bold cyan")
        info.add_column("", min_width=44)
        info.add_row("Path", s.path)
        info.add_row("Tokens", f"{s.token_count:,}  ({_tier_str(s.token_count)})")
        info.add_row("Size", f"{s.size_bytes:,} bytes ({s.size_bytes // 1024}K)")
        info.add_row("Uses", str(s.use_count) if s.use_count else "[dim red]0 — ghost[/]")
        info.add_row("Last", (s.last_used[:19] if s.last_used else "[dim]never[/]"))
        info.add_row("Tag", (s.tag.upper() if s.tag else "[dim]—[/]"))

        desc = s.description or "No description"

        return Group(
            header,
            Panel(info, border_style="dim cyan", padding=(0, 1)),
            Text(f"\n  {desc}\n", style="dim italic"),
        )

    @property
    def _sid(self) -> str:
        return f"{self.skill.agent}:{self.skill.name}"

    def action_do_backup(self) -> None:
        self.dismiss(f"vault|{self._sid}")

    def action_do_delete(self) -> None:
        self.dismiss(f"delete|{self._sid}")

    def action_do_keep(self) -> None:
        self.dismiss(f"keep|{self._sid}")

    def action_do_secure(self) -> None:
        if self.skill.tag == "secure":
            self.dismiss(f"unsecure|{self._sid}")
        else:
            self.dismiss(f"secure|{self._sid}")

    def action_dismiss_detail(self) -> None:
        self.dismiss(None)


class DashboardView(ScrollableContainer):
    def compose(self) -> ComposeResult:
        yield Static(id="dash-ctx")
        with Horizontal(id="dash-row"):
            yield Static(id="dash-chart")
            yield Static(id="dash-leaders")
        yield Static(id="dash-tiers")
        yield Static(id="dash-feed")
        yield Static(id="dash-agents")

    def update_live(self, state: DaemonState, tiers: list[UsageTier]):
        """Called every 1s — refresh timeline, leaderboard, feed, tiers."""
        try:
            cw = self.query_one("#dash-chart").content_size.width - 10
        except Exception:
            cw = 50
        self.query_one("#dash-chart").update(
            _chart(state.timeline_buckets(), max(20, cw))
        )
        self.query_one("#dash-leaders").update(
            _leaderboard(state.top_skills(10))
        )
        self.query_one("#dash-feed").update(
            _feed(state.recent_aggregated(8), state.start_time)
        )
        self.query_one("#dash-tiers").update(
            _usage_breakdown(tiers)
        )

    def update_data(
        self, s: GridStats, breakdown: list[AgentStats], dupes: int,
    ):
        """Called on scan / filter change — refresh stats + agents."""
        bar = _bar(s.budget_pct, 40)
        ghost_hint = ""
        if s.unused > 0:
            ghost_hint = (
                f"  [dim]{s.unused} ghosts — "
                f"[bold]A[/bold]=autopwn  [bold]3[/bold]=HUNT  "
                f"[bold]S[/bold]=secure  [bold]4[/bold]=SECURED[/]"
            )
        self.query_one("#dash-ctx").update(Panel(
            Text.from_markup(
                f"  CTX {bar} {s.budget_pct:.0f}%  "
                f"({s.total_tokens:,} / {CONTEXT_BUDGET:,} tokens)\n"
                f"  [bold]{s.total}[/] skills  "
                f"[bold green]{s.used}[/] active  "
                f"[bold red]{s.unused}[/] ghosts  "
                f"[yellow]K:{s.tagged_keep} R:{s.tagged_remove} DUP:{dupes}[/]"
                f"{ghost_hint}"
            ),
            title="[bold bright_cyan]CONTEXT BUDGET[/]",
            border_style="bright_cyan", padding=(0, 1),
        ))

        # Agents breakdown
        agents_content: Text | Group = Text("")
        if breakdown:
            at = RichTable(show_header=True, expand=True, padding=(0, 1))
            at.add_column("AGENT", min_width=14)
            at.add_column("SKILLS", width=8, justify="right")
            at.add_column("TOKENS", width=10, justify="right")
            at.add_column("", min_width=20)
            mx = max(a.tokens for a in breakdown) or 1
            for a in breakdown:
                glyph, color = AGENT_GLYPHS.get(a.agent, ("?", "white"))
                bw = max(1, int(a.tokens / mx * 18))
                at.add_row(
                    f"[{color}]{glyph} {a.agent}[/]",
                    str(a.count),
                    f"{a.tokens:,}",
                    f"[{color}]{'█' * bw}{'░' * (18 - bw)}[/]",
                )
            agents_content = Panel(
                at, title="[bold bright_cyan]AGENTS[/]",
                border_style="bright_cyan",
            )
        self.query_one("#dash-agents").update(agents_content)


class ArmoryView(Container):
    _ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Input(placeholder="  ⌕  Search skills...  (TAB to cycle agent filter)", id="search")
        yield DataTable(id="grid", cursor_type="row", zebra_stripes=True)

    def on_mount(self):
        t = self.query_one("#grid", DataTable)
        t.add_column("AGENT", width=10, key="agent")
        t.add_column("NAME", width=26, key="name")
        t.add_column("TKN", width=7, key="token_count")
        t.add_column("TIER", width=6, key="tier")
        t.add_column("SIZE", width=7, key="size_bytes")
        t.add_column("USES", width=6, key="use_count")
        t.add_column("LAST", width=12, key="last_used")
        t.add_column("TAG", width=7, key="tag")

    def load_data(self, skills: list[SkillRecord]):
        table = self.query_one("#grid", DataTable)
        cursor = table.cursor_row
        table.clear()
        self._ids = []

        for s in skills:
            sid = f"{s.agent}:{s.name}"
            self._ids.append(sid)

            if s.tag == "keep":
                nm = Text(s.name, style="bold green")
            elif s.tag == "remove":
                nm = Text(s.name, style="red strikethrough")
            else:
                nm = Text(s.name)

            tkn_style = "bold red" if s.token_count > 5000 else (
                "yellow" if s.token_count > 2000 else "cyan"
            )

            table.add_row(
                _agent_badge(s.agent),
                nm,
                Text(f"{s.token_count:,}", style=tkn_style),
                _tier(s.token_count),
                Text(f"{s.size_bytes // 1024}K"),
                Text(str(s.use_count), style="bold bright_green" if s.use_count else "dim"),
                Text(s.last_used[:10] if s.last_used else "GHOST",
                     style="" if s.last_used else "dim red"),
                _tag_text(s.tag),
                key=sid,
            )

        if cursor < table.row_count:
            table.move_cursor(row=cursor)

    def selected_id(self) -> str | None:
        table = self.query_one("#grid", DataTable)
        idx = table.cursor_row
        return self._ids[idx] if 0 <= idx < len(self._ids) else None


class HuntView(Container):
    _ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(id="hunt-header")
        yield Static(id="hunt-agents")
        yield DataTable(id="hunt-grid", cursor_type="row", zebra_stripes=True)
        yield Static(id="hunt-footer")

    def on_mount(self):
        t = self.query_one("#hunt-grid", DataTable)
        t.add_column("", width=2, key="status")
        t.add_column("AGENT", width=10, key="agent")
        t.add_column("SKILL", width=28, key="name")
        t.add_column("USES", width=6, key="uses")
        t.add_column("TKN", width=8, key="tkn")

    def load_skills(
        self,
        all_skills: list[SkillRecord],
        agent_stats: dict[str, tuple[int, int, int]],
        current_filter: str,
    ):
        table = self.query_one("#hunt-grid", DataTable)
        prev_cursor = table.cursor_row
        table.clear()
        self._ids = []

        ghosts = sorted(
            [s for s in all_skills if s.use_count == 0],
            key=lambda s: -s.token_count,
        )
        active = sorted(
            [s for s in all_skills if s.use_count > 0],
            key=lambda s: -s.use_count,
        )

        ghost_tokens = sum(s.token_count for s in ghosts)

        # Ghosts section
        for s in ghosts:
            sid = f"{s.agent}:{s.name}"
            self._ids.append(sid)
            marked = s.tag == "remove"
            tkn_style = "bold red" if s.token_count > 5000 else (
                "yellow" if s.token_count > 2000 else "dim")
            table.add_row(
                Text("☠", style="red"),
                _agent_badge(s.agent),
                Text(s.name, style="bold red" if marked else ""),
                Text("0", style="dim red"),
                Text(f"{s.token_count:,}", style=tkn_style),
                key=sid,
            )

        # Divider row
        if ghosts and active:
            self._ids.append("__divider__")
            table.add_row(
                Text("─", style="dim"),
                Text("──────", style="dim green"),
                Text(f"── ACTIVE ({len(active)}) ─────────", style="bold green"),
                Text("────", style="dim green"),
                Text("──────", style="dim green"),
                key="__divider__",
            )

        # Active section
        for s in active:
            sid = f"{s.agent}:{s.name}"
            self._ids.append(sid)
            table.add_row(
                Text("✓", style="bold bright_green"),
                _agent_badge(s.agent),
                Text(s.name, style="bold bright_green"),
                Text(str(s.use_count), style="bold bright_yellow"),
                Text(f"{s.token_count:,}", style="dim"),
                key=sid,
            )

        if prev_cursor < table.row_count:
            table.move_cursor(row=prev_cursor)

        # Header
        hdr = self.query_one("#hunt-header", Static)
        filter_label = current_filter.upper() if current_filter else "ALL"
        glyph, color = AGENT_GLYPHS.get(current_filter, ("⚔", "bright_magenta"))
        hdr.update(Text.from_markup(
            f"\n  [{color}]{glyph}[/]  [bold bright_magenta]G H O S T   H U N T[/]  "
            f"[bold]{filter_label}[/]  │  "
            f"[bold red]☠ {len(ghosts)}[/] ghosts "
            f"[dim]({ghost_tokens:,} tkn)[/]  │  "
            f"[bold green]✓ {len(active)}[/] active  │  "
            f"[bold bright_yellow]A[/]=AUTOPWN all ☠\n"
        ))

        self._render_agent_bar(agent_stats, current_filter)

        marked_n = sum(1 for s in all_skills if s.tag == "remove")
        ftr = self.query_one("#hunt-footer", Static)
        ftr.update(Text.from_markup(
            f"  [bold red]☠{marked_n}[/] marked  │  "
            f"[bold bright_yellow]A[/]=AUTOPWN  "
            f"[bold bright_cyan]S[/]=SECURE  "
            f"[dim]X=mark P=purge B=vault ENTER=detail U=unmark TAB=agent[/]"
        ))

    def _render_agent_bar(
        self,
        stats: dict[str, tuple[int, int, int]],
        current_filter: str,
    ):
        parts = []
        for agent_id in sorted(stats.keys()):
            total, ghost_n, tokens = stats[agent_id]
            glyph, color = AGENT_GLYPHS.get(agent_id, ("?", "white"))
            active_n = total - ghost_n
            if agent_id == current_filter:
                parts.append(
                    f"[bold reverse {color}] {glyph} {agent_id} "
                    f"☠{ghost_n} ✓{active_n} [/]"
                )
            elif not current_filter:
                parts.append(
                    f"[{color}]{glyph} {agent_id} ☠{ghost_n} ✓{active_n}[/]"
                )
            else:
                parts.append(f"[dim]{glyph} {agent_id}[/]")

        bar_text = "  " + "  │  ".join(parts) if parts else ""
        self.query_one("#hunt-agents", Static).update(Text.from_markup(bar_text))

    def selected_id(self) -> str | None:
        table = self.query_one("#hunt-grid", DataTable)
        idx = table.cursor_row
        if 0 <= idx < len(self._ids):
            sid = self._ids[idx]
            return sid if sid != "__divider__" else None
        return None


class SecuredView(Container):
    _ids: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(id="sec-header")
        yield DataTable(id="sec-grid", cursor_type="row", zebra_stripes=True)
        yield Static(id="sec-footer")

    def on_mount(self):
        t = self.query_one("#sec-grid", DataTable)
        t.add_column("🛡", width=2, key="icon")
        t.add_column("AGENT", width=10, key="agent")
        t.add_column("SKILL", width=28, key="name")
        t.add_column("USES", width=6, key="uses")
        t.add_column("TKN", width=8, key="tkn")
        t.add_column("TAG", width=7, key="tag")

    def load_data(self, secured: list[SkillRecord], total_skills: int):
        table = self.query_one("#sec-grid", DataTable)
        prev_cursor = table.cursor_row
        table.clear()
        self._ids = []

        for s in secured:
            sid = f"{s.agent}:{s.name}"
            self._ids.append(sid)
            tkn_style = "bold red" if s.token_count > 5000 else (
                "yellow" if s.token_count > 2000 else "dim")
            use_style = "bold bright_green" if s.use_count > 0 else "dim red"
            table.add_row(
                Text("🛡", style="bold bright_cyan"),
                _agent_badge(s.agent),
                Text(s.name, style="bold bright_cyan"),
                Text(str(s.use_count), style=use_style),
                Text(f"{s.token_count:,}", style=tkn_style),
                Text("SECURE", style="bold bright_cyan"),
                key=sid,
            )

        if prev_cursor < table.row_count:
            table.move_cursor(row=prev_cursor)

        total_tkn = sum(s.token_count for s in secured)
        hdr = self.query_one("#sec-header", Static)
        hdr.update(Text.from_markup(
            f"\n  [bold bright_cyan]🛡  S E C U R E D[/]  │  "
            f"[bold]{len(secured)}[/] / {total_skills} skills protected  │  "
            f"{total_tkn:,} tokens shielded\n"
            f"  [dim]These skills are immune to AUTOPWN and mass actions[/]\n"
        ))

        ftr = self.query_one("#sec-footer", Static)
        ftr.update(Text.from_markup(
            "  [bold bright_cyan]S[/]=unsecure selected  "
            "[dim]ENTER=detail  TAB=agent filter[/]"
        ))

    def selected_id(self) -> str | None:
        table = self.query_one("#sec-grid", DataTable)
        idx = table.cursor_row
        return self._ids[idx] if 0 <= idx < len(self._ids) else None


class BlastGridApp(App):
    CSS_PATH = "style.tcss"
    TITLE = "BLASTGRID"

    BINDINGS = [
        Binding("1", "go_dashboard", "DASHBOARD", priority=True),
        Binding("2", "go_armory", "ARMORY", priority=True),
        Binding("3", "go_hunt", "HUNT", priority=True),
        Binding("4", "go_secured", "SECURED", priority=True),
        Binding("f5", "do_scan", "SCAN", priority=True),
        Binding("k", "tag_keep", "KEEP", show=False),
        Binding("r", "tag_remove", "REMOVE", show=False),
        Binding("c", "tag_clear", "CLEAR", show=False),
        Binding("x", "mark_ghost", "MARK", show=False),
        Binding("u", "unmark_ghost", "UNMARK", show=False),
        Binding("p", "purge_marked", "PURGE", show=False),
        Binding("b", "vault_selected", "VAULT", show=False),
        Binding("s", "toggle_secure", "SECURE", show=False),
        Binding("a", "autopwn", "AUTOPWN", priority=True),
        Binding("z", "restore_all", "RESTORE", priority=True),
        Binding("tab", "cycle_agent", "FILTER AGENT", show=False),
        Binding("q", "quit", "QUIT"),
    ]

    current_view: reactive[str] = reactive("dashboard")
    agent_filter: reactive[str] = reactive("")
    db: SkillDB | None = None
    _daemon_state: DaemonState | None = None
    _tick: bool = False
    _slow_tick: int = 0
    _last_hits: int = 0
    _sec_count: int = 0

    def compose(self) -> ComposeResult:
        yield Static(id="title-bar")
        yield Static(id="hud")
        with ContentSwitcher(initial="dashboard", id="views"):
            yield DashboardView(id="dashboard")
            yield ArmoryView(id="armory")
            yield HuntView(id="hunt")
            yield SecuredView(id="secured")
        yield Footer()

    def on_mount(self):
        self.db = SkillDB()
        self._agents = [""] + [a.id for a in get_active_agents()]
        self._agent_idx = 0
        self._update_title_bar()
        self._do_scan()
        self._start_daemon()
        self.set_interval(1.0, self._tick_live)

    def _start_daemon(self):
        from . import __main__ as dm

        if not self.db:
            return

        self.db.reset_session()

        state = DaemonState()
        skill_map = dm._build_skill_file_map(self.db)
        patterns = dm._get_skill_regex_patterns()
        watch_dirs = [str(d) for _, d in get_all_skill_dirs()]

        state.monitoring = len(skill_map)

        dm._daemon_state = state
        dm._live_mode = True
        dm._session_start = time.time()
        dm._session_hits = 0
        dm._session_unique = set()

        is_mac = platform.system() == "Darwin"
        is_linux = platform.system() == "Linux"
        is_root = os.geteuid() == 0 if hasattr(os, "geteuid") else False

        if is_mac and is_root:
            state.backend = "fs_usage"
            fn = lambda: dm._daemon_fs_usage(self.db, patterns)
        elif is_linux and shutil.which("inotifywait"):
            state.backend = "inotifywait"
            fn = lambda: dm._daemon_inotifywait(self.db, patterns, watch_dirs)
        else:
            state.backend = "atime-poll"
            fn = lambda: dm._daemon_python(self.db, skill_map)

        def _bg():
            try:
                fn()
            except Exception as e:
                state.error = str(e)

        threading.Thread(target=_bg, daemon=True).start()
        self._daemon_state = state

    def _tick_live(self):
        """Called every 1s — update live portions of the UI."""
        self._tick = not self._tick
        self._slow_tick += 1
        state = self._daemon_state

        if state and self.db and self.current_view == "dashboard":
            tiers = self.db.get_usage_tiers(agent_filter=self.agent_filter)
            self.query_one("#dashboard", DashboardView).update_live(state, tiers)

        # Refresh tables when new hits arrive (or every 5s as fallback)
        cur_hits = state.total if state else 0
        data_changed = cur_hits != self._last_hits
        slow_due = self._slow_tick % 5 == 0
        if self.db and (data_changed or slow_due):
            self._last_hits = cur_hits
            self._refresh_all()

        if state and self.db:
            stats = self.db.get_stats(agent_filter=self.agent_filter)
            self._update_hud(stats, state)

    def watch_current_view(self, value: str):
        self.query_one("#views", ContentSwitcher).current = value
        self._update_title_bar()

    def action_go_dashboard(self): self.current_view = "dashboard"
    def action_go_armory(self):    self.current_view = "armory"
    def action_go_hunt(self):      self.current_view = "hunt"
    def action_go_secured(self):   self.current_view = "secured"

    def action_cycle_agent(self):
        self._agent_idx = (self._agent_idx + 1) % len(self._agents)
        self.agent_filter = self._agents[self._agent_idx]

    def watch_agent_filter(self, value: str):
        self._refresh_all()
        label = value or "ALL"
        self.notify(f"Filter: {label}", severity="information")

    def action_do_scan(self):
        self._do_scan()

    def _do_scan(self):
        skills = scan_all_agents()
        if skills and self.db:
            self.db.upsert_skills(skills)
        agents_found = len({s.agent for s in skills})
        self._refresh_all()
        self.notify(
            f"⚡ Scanned {len(skills)} skills across {agents_found} agents",
            severity="information",
        )

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self.current_view == "hunt":
            sid = self.query_one("#hunt", HuntView).selected_id()
        elif self.current_view == "armory":
            sid = self.query_one("#armory", ArmoryView).selected_id()
        elif self.current_view == "secured":
            sid = self.query_one("#secured", SecuredView).selected_id()
        else:
            return

        if sid and self.db:
            skill = self.db.get_by_id(sid)
            if skill:
                self.push_screen(SkillDetail(skill), self._on_detail_result)

    def _on_detail_result(self, result) -> None:
        if not result:
            return
        parts = str(result).split("|", 1)
        if len(parts) != 2:
            return
        action, sid = parts
        name = sid.split(":", 1)[-1]

        if action == "vault":
            self._vault_skill(sid)
        elif action == "delete":
            self._graveyard_skill(sid)
        elif action == "keep":
            if self.db:
                self.db.tag_skill(sid, "keep")
                self.notify(f"✦ {name} → KEEP", severity="information")
        elif action == "secure":
            if self.db:
                self.db.tag_skill(sid, "secure")
                self.notify(f"🛡 {name} → SECURED", severity="information")
        elif action == "unsecure":
            if self.db:
                self.db.tag_skill(sid, None)
                self.notify(f"🔓 {name} unsecured", severity="information")
        self._refresh_all()

    def _vault_skill(self, sid: str) -> None:
        if not self.db:
            return
        skill = self.db.get_by_id(sid)
        if not skill:
            return
        src = Path(skill.path)
        VAULT.mkdir(parents=True, exist_ok=True)
        dest = VAULT / f"{skill.agent}__{skill.name}"
        if dest.exists():
            dest = VAULT / f"{skill.agent}__{skill.name}_{int(time.time())}"
        if src.is_dir():
            shutil.move(str(src), str(dest))
        self.db.delete_skill(sid)
        self.notify(f"📦 {skill.name} → VAULT (restorable)", severity="information")

    def _graveyard_skill(self, sid: str) -> None:
        if not self.db:
            return
        skill = self.db.get_by_id(sid)
        if not skill:
            return
        src = Path(skill.path)
        GRAVEYARD.mkdir(parents=True, exist_ok=True)
        dest = GRAVEYARD / f"{skill.agent}__{skill.name}"
        if dest.exists():
            dest = GRAVEYARD / f"{skill.agent}__{skill.name}_{int(time.time())}"
        if src.is_dir():
            shutil.move(str(src), str(dest))
        self.db.delete_skill(sid)
        self.notify(f"⚔ {skill.name} → GRAVEYARD", severity="warning")

    def action_vault_selected(self):
        if self.current_view == "hunt":
            sid = self.query_one("#hunt", HuntView).selected_id()
        elif self.current_view == "armory":
            sid = self.query_one("#armory", ArmoryView).selected_id()
        elif self.current_view == "secured":
            sid = self.query_one("#secured", SecuredView).selected_id()
        else:
            return
        if sid:
            self._vault_skill(sid)
            self._refresh_all()

    def action_tag_keep(self):
        if self.current_view == "armory":
            self._tag("keep")

    def action_tag_remove(self):
        if self.current_view == "armory":
            self._tag("remove")

    def action_tag_clear(self):
        if self.current_view == "armory":
            self._tag(None)

    def _tag(self, tag: str | None):
        armory = self.query_one("#armory", ArmoryView)
        sid = armory.selected_id()
        if sid and self.db:
            self.db.tag_skill(sid, tag)
            label = tag.upper() if tag else "CLEARED"
            icon = "✦" if tag == "keep" else ("☠" if tag == "remove" else "○")
            name = sid.split(":", 1)[-1]
            self.notify(f"{icon} {name} → {label}")
            self._refresh_all()

    def action_mark_ghost(self):
        if self.current_view == "hunt":
            hunt = self.query_one("#hunt", HuntView)
            sid = hunt.selected_id()
            if sid and self.db:
                self.db.tag_skill(sid, "remove")
                name = sid.split(":", 1)[-1]
                self.notify(f"☠ Marked: {name}", severity="warning")
                self._refresh_all()

    def action_unmark_ghost(self):
        if self.current_view == "hunt":
            hunt = self.query_one("#hunt", HuntView)
            sid = hunt.selected_id()
            if sid and self.db:
                self.db.tag_skill(sid, None)
                name = sid.split(":", 1)[-1]
                self.notify(f"○ Unmarked: {name}")
                self._refresh_all()

    def action_purge_marked(self):
        if self.current_view != "hunt" or not self.db:
            return
        all_sk = self.db.get_all(agent_filter=self.agent_filter)
        marked = [g for g in all_sk if g.tag == "remove"]
        if not marked:
            self.notify("No marked ghosts", severity="warning")
            return

        GRAVEYARD.mkdir(parents=True, exist_ok=True)
        freed = 0
        for g in marked:
            src = Path(g.path)
            dest = GRAVEYARD / f"{g.agent}__{g.name}"
            if dest.exists():
                dest = GRAVEYARD / f"{g.agent}__{g.name}_{int(time.time())}"
            if src.is_dir():
                shutil.move(str(src), str(dest))
            sid = self.db.skill_id(g.agent, g.name)
            self.db.delete_skill(sid)
            freed += g.token_count

        self.notify(
            f"⚔ VANQUISHED {len(marked)} ghosts! {freed:,} tokens freed",
            severity="information",
        )
        self._refresh_all()

    def action_toggle_secure(self):
        sid = None
        if self.current_view == "armory":
            sid = self.query_one("#armory", ArmoryView).selected_id()
        elif self.current_view == "hunt":
            sid = self.query_one("#hunt", HuntView).selected_id()
        elif self.current_view == "secured":
            sid = self.query_one("#secured", SecuredView).selected_id()
        if not sid or not self.db:
            return
        skill = self.db.get_by_id(sid)
        if not skill:
            return
        name = sid.split(":", 1)[-1]
        if skill.tag == "secure":
            self.db.tag_skill(sid, None)
            self.notify(f"🔓 {name} unsecured", severity="information")
        else:
            self.db.tag_skill(sid, "secure")
            self.notify(f"🛡 {name} → SECURED (immune to AUTOPWN)", severity="information")
        self._refresh_all()

    def action_autopwn(self):
        if not self.db:
            return
        self.push_screen(
            AutopwnModal(self.db, self.agent_filter),
            self._on_autopwn_result,
        )

    def _on_autopwn_result(self, result) -> None:
        if not result or not self.db:
            return
        action, threshold = result
        if action != "autopwn":
            return
        candidates = self.db.get_autopwn_candidates(
            threshold, self.agent_filter
        )
        VAULT.mkdir(parents=True, exist_ok=True)
        moved = 0
        freed = 0
        for g in candidates:
            src = Path(g.path)
            dest = VAULT / f"{g.agent}__{g.name}"
            if dest.exists():
                dest = VAULT / f"{g.agent}__{g.name}_{int(time.time())}"
            try:
                if src.is_dir():
                    shutil.move(str(src), str(dest))
                sid = self.db.skill_id(g.agent, g.name)
                self.db.delete_skill(sid)
                moved += 1
                freed += g.token_count
            except Exception:
                pass
        self._refresh_all()
        self.notify(
            f"AUTOPWN: {moved} skills (<{threshold} uses) vaulted, "
            f"{freed:,} tokens freed! Press Z to restore.",
            severity="information",
        )

    def action_restore_all(self):
        if not VAULT.is_dir():
            self.notify("Vault is empty", severity="warning")
            return
        items = sorted(VAULT.iterdir())
        if not items:
            self.notify("Vault is empty", severity="warning")
            return
        self.push_screen(
            ConfirmModal(
                "RESTORE ALL — UNDO VAULT",
                f"Restore {len(items)} skills from vault back to their agent directories?\n"
                f"  All vaulted skills will return to their original locations.",
                "restore_all",
            ),
            self._on_restore_all_result,
        )

    def _on_restore_all_result(self, result) -> None:
        if result != "restore_all":
            return
        from .agents import get_agent
        if not VAULT.is_dir():
            return
        items = sorted(VAULT.iterdir())
        restored = 0
        failed = 0
        for item in items:
            if not item.is_dir():
                continue
            parts = item.name.split("__", 1)
            if len(parts) != 2:
                failed += 1
                continue
            agent_id, skill_name = parts
            if "_" in skill_name and skill_name.rsplit("_", 1)[-1].isdigit():
                skill_name = skill_name.rsplit("_", 1)[0]
            agent_def = get_agent(agent_id)
            if not agent_def:
                failed += 1
                continue
            dest = agent_def.global_dirs[0] / skill_name
            if dest.exists():
                failed += 1
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(item), str(dest))
                restored += 1
            except Exception:
                failed += 1
        self._do_scan()
        msg = f"Restored {restored} skills"
        if failed:
            msg += f" ({failed} failed)"
        self.notify(msg, severity="information")

    def on_input_changed(self, event: Input.Changed):
        if event.input.id == "search" and self.db:
            skills = self.db.get_all(search=event.value, agent_filter=self.agent_filter)
            self.query_one("#armory", ArmoryView).load_data(skills)

    def _refresh_all(self):
        if not self.db:
            return
        af = self.agent_filter
        stats = self.db.get_stats(agent_filter=af)
        breakdown = self.db.get_agent_breakdown()
        dupes = len(self.db.get_duplicates())
        all_skills = self.db.get_all(
            sort_by="use_count", desc=True, agent_filter=af
        )

        # Agent stats for Hunt bar: (total, ghosts, ghost_tokens)
        all_skills_unfiltered = self.db.get_all() if af else all_skills
        agent_hunt_stats: dict[str, tuple[int, int, int]] = {}
        for s in all_skills_unfiltered:
            total, ghosts, tkn = agent_hunt_stats.get(s.agent, (0, 0, 0))
            total += 1
            if s.use_count == 0:
                ghosts += 1
                tkn += s.token_count
            agent_hunt_stats[s.agent] = (total, ghosts, tkn)

        secured = self.db.get_secured(agent_filter=af)
        self._sec_count = len(secured)

        self.query_one("#dashboard", DashboardView).update_data(
            stats, breakdown, dupes,
        )
        self.query_one("#armory", ArmoryView).load_data(all_skills)
        self.query_one("#hunt", HuntView).load_skills(
            all_skills, agent_hunt_stats, af,
        )
        self.query_one("#secured", SecuredView).load_data(
            secured, stats.total,
        )

    def _update_title_bar(self):
        views = [
            ("dashboard", "1:DASHBOARD"), ("armory", "2:ARMORY"),
            ("hunt", "3:HUNT"), ("secured", "4:SECURED"),
        ]
        tabs = []
        for key, label in views:
            if key == self.current_view:
                tabs.append(f"[bold reverse bright_cyan] {label} [/]")
            else:
                tabs.append(f"[dim] {label} [/]")
        bar = f"[bold bright_white] ◆ BLASTGRID ◆ [/]│{'│'.join(tabs)}│"
        self.query_one("#title-bar", Static).update(Text.from_markup(bar))

    def _update_hud(self, s: GridStats, state: DaemonState | None = None):
        bar = _bar(s.budget_pct, 16)
        af = self.agent_filter
        agent_label = f"[bold]{af}[/]" if af else "ALL"

        if state:
            pulse = "●" if self._tick else "○"
            p_style = "bold bright_green" if self._tick else "dim green"
            live_part = (
                f"[{p_style}]{pulse}[/] "
                f"[bold bright_white]⏱ {state.elapsed}[/]  "
                f"[bold bright_yellow]⚡{state.total}[/]  "
                f"[bold bright_green]◆{state.unique}[/]  "
            )
        else:
            live_part = ""

        sec_n = self._sec_count
        hud = (
            f" {live_part}"
            f"{agent_label}  CTX {bar} {s.budget_pct:.0f}%"
            f"  │  [bold]T[/]{s.total}"
            f"  │  [bold green]A[/]{s.used}"
            f"  │  [bold red]G[/]{s.unused}"
            f"  │  [bold bright_cyan]🛡[/]{sec_n}"
            f"  │  [dim]A=autopwn S=secure Z=restore[/]"
        )
        self.query_one("#hud", Static).update(Text.from_markup(hud))
