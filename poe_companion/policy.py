"""Shared compositor-independent window matching and campaign progress policy."""
import re


def _matches(pattern: str, text: str) -> bool:
    if not isinstance(pattern, str) or not isinstance(text, str):
        return False
    try:
        return bool(re.search(pattern, text))
    except (re.error, TypeError):
        return False


DEFAULT_GAME_APP_ID_REGEX = (
    r"(?i)^(?:steam_app_238960|pathofexile(?:_x64)?(?:steam)?(?:client)?(?:\.exe)?|"
    r"org\.pathofexile\.pathofexile)$"
)
DEFAULT_GAME_TITLE_REGEX = r"(?i)^path of exile$"
DEFAULT_LEGACY_APP_ID_REGEX = r"(?i)^(?:wine|wine64)$"
GENERIC_STEAM_CLIENT_REGEX = r"(?i)^(?:steam(?:\.exe)?|steamwebhelper(?:\.exe)?|com\.valvesoftware\.steam)$"
STEAM_APP_ID_REGEX = r"(?i)^steam_app_(\d+)$"


def is_game_focus(info: dict, config: dict | None = None) -> bool:
    """Match known PoE 1 window IDs; allow exact titles only for unnamed/legacy windows."""
    config = config if isinstance(config, dict) else {}
    identity_value = info.get("app_id", "") if isinstance(info, dict) else ""
    title_value = info.get("title", "") if isinstance(info, dict) else ""
    identity = identity_value.strip() if isinstance(identity_value, str) else ""
    title = title_value.strip() if isinstance(title_value, str) else ""
    explicit = config.get("game_window_regex")
    if not isinstance(explicit, str):
        explicit = None
    allow_generic_steam = config.get("allow_steam_window_identity") is True
    if explicit:
        matched = _matches(explicit, identity + " " + title)
    else:
        app_re = config.get("game_app_id_regex")
        title_re = config.get("game_title_regex")
        legacy_re = config.get("legacy_game_app_id_regex")
        app_re = app_re if isinstance(app_re, str) else DEFAULT_GAME_APP_ID_REGEX
        title_re = title_re if isinstance(title_re, str) else DEFAULT_GAME_TITLE_REGEX
        legacy_re = legacy_re if isinstance(legacy_re, str) else DEFAULT_LEGACY_APP_ID_REGEX
        known_id = _matches(app_re, identity)
        safe_title_fallback = (not identity or _matches(legacy_re, identity)
                               or (_matches(GENERIC_STEAM_CLIENT_REGEX, identity) and allow_generic_steam))
        matched = known_id or (safe_title_fallback and _matches(title_re, title))

    steam_app = re.fullmatch(STEAM_APP_ID_REGEX, identity)
    other_steam_app = steam_app is not None and steam_app.group(1) != "238960"
    generic_steam_client = _matches(GENERIC_STEAM_CLIENT_REGEX, identity)
    generic_steam_allowed = allow_generic_steam and _matches(DEFAULT_GAME_TITLE_REGEX, title)
    return bool(isinstance(info, dict) and info.get("focused") is True and matched
                and not other_steam_app and (not generic_steam_client or generic_steam_allowed))


def toggle_clickable(current: bool, focus: dict, config: dict | None = None) -> bool | None:
    """Return the new state only when the currently focused window is PoE."""
    return not current if is_game_focus(focus, config) else None


def advance(campaign: list, act: int, step: int, delta: int) -> tuple[int, int]:
    act = min(max(act, 0), len(campaign)-1)
    step = min(max(step, 0), len(campaign[act]["steps"])-1) + delta
    if step < 0:
        if act > 0:
            act -= 1
            step = len(campaign[act]["steps"])-1
        else:
            step = 0
    elif step >= len(campaign[act]["steps"]):
        if act < len(campaign)-1:
            act += 1
            step = 0
        else:
            step = len(campaign[act]["steps"])-1
    return act, step
