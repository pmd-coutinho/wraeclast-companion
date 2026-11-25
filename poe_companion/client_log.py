"""Incremental PoE 1 Client.txt zone-entry tailer for automatic campaign step tracking."""
from __future__ import annotations

import re
from pathlib import Path

_ENTERED_ZONE_RE = re.compile(r":\s*You have entered ([^.\r\n]+)\.\s*$")
_LEVEL_SUFFIX_RE = re.compile(r"\s+level\s+\d+$", re.IGNORECASE)
_VDF_PATH_RE = re.compile(r'"path"\s+"([^"]+)"')
_MAX_VDF_BYTES = 256 * 1024

_DEFAULT_STEAM_ROOTS = (
    Path(".local/share/Steam"),
    Path(".steam/steam"),
    Path(".steam/root"),
    Path(".var/app/com.valvesoftware.Steam/.local/share/Steam"),
)
_POE_REL_LOG = Path("steamapps/common/Path of Exile/logs/Client.txt")


def parse_entered_zone(line: str) -> str | None:
    """Extract the entered zone name from a PoE 1 Client.txt log line."""
    if not isinstance(line, str) or "You have entered " not in line:
        return None
    match = _ENTERED_ZONE_RE.search(line)
    if not match:
        return None
    zone = match.group(1).strip()
    return zone or None


def _steam_library_roots(home: Path) -> list[Path]:
    roots: list[Path] = []
    seen: set[Path] = set()
    for rel in _DEFAULT_STEAM_ROOTS:
        steam_root = home / rel
        if steam_root not in seen:
            seen.add(steam_root)
            roots.append(steam_root)
        vdf = steam_root / "steamapps/libraryfolders.vdf"
        try:
            if not vdf.is_file() or vdf.stat().st_size > _MAX_VDF_BYTES:
                continue
            text = vdf.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for match in _VDF_PATH_RE.finditer(text):
            raw_path = match.group(1).replace("\\\\", "\\")
            lib_root = Path(raw_path).expanduser()
            if lib_root not in seen:
                seen.add(lib_root)
                roots.append(lib_root)
    return roots


def discover_client_log(configured_path: str | None = None, home: Path | None = None) -> Path | None:
    """Locate an existing PoE 1 Client.txt log file from config or Steam libraries."""
    if configured_path:
        candidate = Path(configured_path).expanduser()
        if candidate.is_file():
            return candidate
    base = home if home is not None else Path.home()
    for root in _steam_library_roots(base):
        candidate = root / _POE_REL_LOG
        if candidate.is_file():
            return candidate
    return None


def _zone_variants(name: str) -> tuple[str, ...]:
    cleaned = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if cleaned.startswith("the "):
        cleaned = cleaned[4:]
    if not cleaned:
        return ()
    base = _LEVEL_SUFFIX_RE.sub("", cleaned).strip()
    if base and base != cleaned:
        return (cleaned, base)
    return (cleaned,)


def _mentions_zone(text: str, zone_variants: tuple[str, ...]) -> bool:
    haystack = (text or "").lower()
    for variant in zone_variants:
        if len(variant) < 4:
            continue
        if re.search(rf"(?<!\w){re.escape(variant)}(?!\w)", haystack):
            return True
    return False


def match_campaign_step(campaign: list, current_act: int, current_step: int,
                        zone_name: str) -> tuple[int, int] | None:
    """Match an entered zone to the next forward step in the current or next act."""
    variants = _zone_variants(zone_name)
    if not variants or not campaign:
        return None
    act_idx = min(max(current_act, 0), len(campaign) - 1)

    # First check if the player entered the next act's town/title zone.
    if act_idx + 1 < len(campaign):
        next_act = campaign[act_idx + 1]
        if _mentions_zone(next_act.get("title", ""), variants):
            return act_idx + 1, 0

    # Check forward steps in the current act (bounded to avoid skipping over the act).
    steps = campaign[act_idx].get("steps", [])
    for step_idx in range(current_step + 1, len(steps)):
        step = steps[step_idx]
        if _mentions_zone(step.get("title", ""), variants) or _mentions_zone(step.get("detail", ""), variants):
            return act_idx, step_idx

    # Check the first two steps of the next act in case the player moved straight past town.
    if act_idx + 1 < len(campaign):
        next_steps = campaign[act_idx + 1].get("steps", [])
        for step_idx in range(min(2, len(next_steps))):
            step = next_steps[step_idx]
            if _mentions_zone(step.get("title", ""), variants) or _mentions_zone(step.get("detail", ""), variants):
                return act_idx + 1, step_idx
    return None


class ClientLogTailer:
    """Bounded incremental reader that tails newly appended Client.txt lines without full reads."""

    def __init__(self, path: Path):
        self.path = path
        self._inode: int | None = None
        self._offset: int = 0
        self._partial: str = ""
        self._sync_to_end()

    def _sync_to_end(self):
        try:
            st = self.path.stat()
            self._inode = st.st_ino
            self._offset = st.st_size
            self._partial = ""
        except OSError:
            self._inode = None
            self._offset = 0
            self._partial = ""

    def poll_zones(self, max_bytes: int = 65536) -> list[str]:
        try:
            st = self.path.stat()
        except OSError:
            return []
        if self._inode is None:
            self._inode = st.st_ino
            self._offset = st.st_size
            return []
        if st.st_ino != self._inode or st.st_size < self._offset:
            self._inode = st.st_ino
            self._offset = 0
            self._partial = ""
        if st.st_size == self._offset:
            return []
        to_read = min(max_bytes, st.st_size - self._offset)
        try:
            with self.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read(to_read)
        except OSError:
            return []
        self._offset += len(chunk)
        text = self._partial + chunk.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
        if lines and not lines[-1].endswith(("\n", "\r")):
            self._partial = lines.pop()
        else:
            self._partial = ""
        zones = []
        for line in lines:
            zone = parse_entered_zone(line)
            if zone:
                zones.append(zone)
        return zones
