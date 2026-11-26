"""GTK-free StatusNotifierItem / dbusmenu data for the Wraeclast Companion tray icon."""
from __future__ import annotations

TRAY_TITLE = "Wraeclast Companion"
TRAY_ICON_NAME = "applications-games"
TRAY_ITEM_ID = "wraeclast-companion"
ITEM_INTERFACE = "org.kde.StatusNotifierItem"
ITEM_PATH = "/StatusNotifierItem"
WATCHER_NAME = "org.kde.StatusNotifierWatcher"
WATCHER_PATH = "/StatusNotifierWatcher"
WATCHER_INTERFACE = "org.kde.StatusNotifierWatcher"
MENU_INTERFACE = "com.canonical.dbusmenu"
MENU_PATH = "/MenuBar"
PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
MENU_ROOT_ID = 0
MENU_ID_TOGGLE = 1
MENU_ID_SEPARATOR = 2
MENU_ID_QUIT = 3
MENU_ITEM_IDS = (MENU_ID_TOGGLE, MENU_ID_SEPARATOR, MENU_ID_QUIT)
MAX_MENU_EVENTS = 16
MAX_MENU_IDS = 64
ACTION_TOGGLE_PLANNER = "toggle-planner"
ACTION_QUIT = "quit"

ITEM_PROPERTIES = {
    "Category": ("s", "ApplicationStatus"),
    "Id": ("s", TRAY_ITEM_ID),
    "Title": ("s", TRAY_TITLE),
    "Status": ("s", "Active"),
    "WindowId": ("i", 0),
    "IconName": ("s", TRAY_ICON_NAME),
    "IconPixmap": ("a(iiay)", []),
    "OverlayIconName": ("s", ""),
    "OverlayIconPixmap": ("a(iiay)", []),
    "AttentionIconName": ("s", ""),
    "AttentionIconPixmap": ("a(iiay)", []),
    "AttentionMovieName": ("s", ""),
    "ToolTip": ("(sa(iiay)ss)", (TRAY_ICON_NAME, [], TRAY_TITLE, "")),
    "ItemIsMenu": ("b", False),
    "Menu": ("o", MENU_PATH),
    "IconThemePath": ("s", ""),
}
MENU_PROPERTIES = {
    "Version": ("u", 3),
    "TextDirection": ("s", "ltr"),
    "Status": ("s", "normal"),
    "IconThemePath": ("as", []),
}


def _properties_xml(properties: dict) -> str:
    return "".join(f'<property name="{name}" type="{sig}" access="read"/>' for name, (sig, _value) in properties.items())


ITEM_INTERFACE_XML = (
    '<node><interface name="org.kde.StatusNotifierItem">'
    '<method name="Activate">'
    '<arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>'
    "</method>"
    '<method name="SecondaryActivate">'
    '<arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>'
    "</method>"
    '<method name="ContextMenu">'
    '<arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/>'
    "</method>"
    '<method name="Scroll">'
    '<arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/>'
    "</method>"
    '<signal name="NewTitle"/>'
    '<signal name="NewIcon"/>'
    '<signal name="NewAttentionIcon"/>'
    '<signal name="NewOverlayIcon"/>'
    '<signal name="NewToolTip"/>'
    '<signal name="NewStatus"><arg name="status" type="s"/></signal>'
    + _properties_xml(ITEM_PROPERTIES)
    + "</interface></node>"
)

MENU_INTERFACE_XML = (
    '<node><interface name="com.canonical.dbusmenu">'
    '<method name="GetLayout">'
    '<arg name="parentId" type="i" direction="in"/>'
    '<arg name="recursionDepth" type="i" direction="in"/>'
    '<arg name="propertyNames" type="as" direction="in"/>'
    '<arg name="revision" type="u" direction="out"/>'
    '<arg name="layout" type="(ia{sv}av)" direction="out"/>'
    "</method>"
    '<method name="GetGroupProperties">'
    '<arg name="ids" type="ai" direction="in"/>'
    '<arg name="propertyNames" type="as" direction="in"/>'
    '<arg name="properties" type="a(ia{sv})" direction="out"/>'
    "</method>"
    '<method name="GetProperty">'
    '<arg name="id" type="i" direction="in"/>'
    '<arg name="name" type="s" direction="in"/>'
    '<arg name="value" type="v" direction="out"/>'
    "</method>"
    '<method name="Event">'
    '<arg name="id" type="i" direction="in"/>'
    '<arg name="eventId" type="s" direction="in"/>'
    '<arg name="data" type="v" direction="in"/>'
    '<arg name="timestamp" type="u" direction="in"/>'
    "</method>"
    '<method name="EventGroup">'
    '<arg name="events" type="a(isvu)" direction="in"/>'
    '<arg name="idErrors" type="ai" direction="out"/>'
    "</method>"
    '<method name="AboutToShow">'
    '<arg name="id" type="i" direction="in"/>'
    '<arg name="needUpdate" type="b" direction="out"/>'
    "</method>"
    '<method name="AboutToShowGroup">'
    '<arg name="ids" type="ai" direction="in"/>'
    '<arg name="updatesNeeded" type="ai" direction="out"/>'
    '<arg name="idErrors" type="ai" direction="out"/>'
    "</method>"
    '<signal name="ItemsPropertiesUpdated">'
    '<arg name="updatedProps" type="a(ia{sv})"/>'
    '<arg name="removedProps" type="a(ias)"/>'
    "</signal>"
    '<signal name="LayoutUpdated">'
    '<arg name="revision" type="u"/>'
    '<arg name="parent" type="i"/>'
    "</signal>"
    '<signal name="ItemActivationRequested">'
    '<arg name="id" type="i"/>'
    '<arg name="timestamp" type="u"/>'
    "</signal>"
    + _properties_xml(MENU_PROPERTIES)
    + "</interface></node>"
)


def item_bus_name(pid: int) -> str:
    return f"org.kde.StatusNotifierItem-{int(pid)}-1"


def toggle_label(planner_visible: bool) -> str:
    return "Hide planner" if planner_visible else "Show planner"


def menu_items(planner_visible: bool) -> list[tuple[int, dict]]:
    return [
        (MENU_ID_TOGGLE, {"label": toggle_label(planner_visible)}),
        (MENU_ID_SEPARATOR, {"type": "separator"}),
        (MENU_ID_QUIT, {"label": "Quit"}),
    ]


def menu_layout(planner_visible: bool, parent_id: int = MENU_ROOT_ID) -> tuple[int, dict, list] | None:
    if parent_id == MENU_ROOT_ID:
        children = [(item_id, props, []) for item_id, props in menu_items(planner_visible)]
        return (MENU_ROOT_ID, {"children-display": "submenu"}, children)
    for item_id, props in menu_items(planner_visible):
        if item_id == parent_id:
            return (item_id, props, [])
    return None


def menu_item_properties(planner_visible: bool, ids) -> list[tuple[int, dict]]:
    items = menu_items(planner_visible)
    if not ids:
        return items
    wanted = set(list(ids)[:MAX_MENU_IDS])
    return [(item_id, props) for item_id, props in items if item_id in wanted]


def menu_action(item_id: int, event_id: str) -> str | None:
    if event_id != "clicked":
        return None
    if item_id == MENU_ID_TOGGLE:
        return ACTION_TOGGLE_PLANNER
    if item_id == MENU_ID_QUIT:
        return ACTION_QUIT
    return None
