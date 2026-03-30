from __future__ import annotations

import shutil
import time
from pathlib import Path


def stash_in_vault(src: Path, vault_root: Path, agent: str, name: str) -> Path | None:
    """Move a skill directory or a single watched file into vault_root/agent__name/."""
    try:
        vault_root.mkdir(parents=True, exist_ok=True)
        dest = vault_root / f"{agent}__{name}"
        if dest.exists():
            dest = vault_root / f"{agent}__{name}~~{int(time.time())}"
        if src.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            (dest / ".blastgrid-origin").write_text(
                str(src.resolve()), encoding="utf-8"
            )
            shutil.move(str(src), str(dest / src.name))
        elif src.is_dir():
            shutil.move(str(src), str(dest))
        else:
            return None
        return dest
    except OSError:
        return None


def restore_watch_vault_folder(vault_folder: Path) -> bool:
    """Restore a watch.conf file vaulted under watch__w*/ containing .blastgrid-origin."""
    origin = vault_folder / ".blastgrid-origin"
    if not origin.is_file():
        return False
    target = Path(origin.read_text(encoding="utf-8").strip())
    candidates = [
        f for f in vault_folder.iterdir() if f.name != ".blastgrid-origin"
    ]
    if len(candidates) != 1 or not candidates[0].is_file():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return False
    shutil.move(str(candidates[0]), str(target))
    shutil.rmtree(vault_folder)
    return True
