"""Tux's icon in the top bar / system tray: click it to call him up or put him away.

Speaks the StatusNotifierItem + dbusmenu D-Bus protocols directly (the ones Ubuntu's built-in
AppIndicator extension, KDE, XFCE, Cinnamon and others show). GTK4 has no tray widget and
libappindicator is GTK3-only, so this needs nothing extra installed.
"""
import os

import cairo
from gi.repository import Gio, GLib

WATCHER = "org.kde.StatusNotifierWatcher"
ITEM_PATH = "/StatusNotifierItem"
MENU_PATH = "/MenuBar"

ITEM_XML = """<node><interface name="org.kde.StatusNotifierItem">
<property name="Category" type="s" access="read"/>
<property name="Id" type="s" access="read"/>
<property name="Title" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="WindowId" type="i" access="read"/>
<property name="IconName" type="s" access="read"/>
<property name="IconPixmap" type="a(iiay)" access="read"/>
<property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
<property name="ItemIsMenu" type="b" access="read"/>
<property name="Menu" type="o" access="read"/>
<method name="Activate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
<method name="SecondaryActivate"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
<method name="ContextMenu"><arg name="x" type="i" direction="in"/><arg name="y" type="i" direction="in"/></method>
<method name="Scroll"><arg name="delta" type="i" direction="in"/><arg name="orientation" type="s" direction="in"/></method>
<signal name="NewIcon"/><signal name="NewToolTip"/><signal name="NewStatus"><arg type="s"/></signal>
</interface></node>"""

MENU_XML = """<node><interface name="com.canonical.dbusmenu">
<property name="Version" type="u" access="read"/>
<property name="TextDirection" type="s" access="read"/>
<property name="Status" type="s" access="read"/>
<property name="IconThemePath" type="as" access="read"/>
<method name="GetLayout"><arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
  <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/></method>
<method name="GetGroupProperties"><arg type="ai" direction="in"/><arg type="as" direction="in"/>
  <arg type="a(ia{sv})" direction="out"/></method>
<method name="GetProperty"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/></method>
<method name="Event"><arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/></method>
<method name="EventGroup"><arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/></method>
<method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
<method name="AboutToShowGroup"><arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/></method>
<signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
<signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
</interface></node>"""


def icon_pixmaps(png, sizes=(22, 32, 48)):
    """The picture as SNI pixmaps: ARGB32 in network byte order, one per size."""
    try:
        art = cairo.ImageSurface.create_from_png(png)
    except Exception:
        return []
    out = []
    for size in sizes:
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        cr = cairo.Context(surf)
        k = size / max(art.get_width(), art.get_height())
        cr.translate((size - art.get_width() * k) / 2, (size - art.get_height() * k) / 2)
        cr.scale(k, k)
        cr.set_source_surface(art, 0, 0)
        cr.get_source().set_filter(cairo.FILTER_BEST)
        cr.paint()
        surf.flush()
        stride, data = surf.get_stride(), bytes(surf.get_data())
        rows = b"".join(data[y * stride:y * stride + size * 4] for y in range(size))
        argb = bytearray(len(rows))
        if os.sys.byteorder == "little":  # cairo stores BGRA here; SNI wants ARGB
            argb[0::4], argb[1::4], argb[2::4], argb[3::4] = rows[3::4], rows[2::4], rows[1::4], rows[0::4]
        else:
            argb[:] = rows
        out.append((size, size, bytes(argb)))
    return out


class Tray:
    """menu: [(label, callback) or None for a line]; on_click(x, y): left click on the icon,
    at that screen spot (0, 0 if the tray host doesn't say).
    on_change(shown): called when a tray host appears or goes away (True = icon is up)."""

    def __init__(self, icon_png, on_click, menu, on_change=None, title="Tux"):
        self.on_click, self.menu, self.on_change, self.title = on_click, menu, on_change, title
        self.pixmaps = icon_pixmaps(icon_png)
        self.bus = None
        self.ids = []
        self.name_id = self.watch_id = 0
        self.shown = False
        self.service = f"org.kde.StatusNotifierItem-{os.getpid()}-1"

    # ---- lifecycle
    def start(self):
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except GLib.Error:
            return False
        item = Gio.DBusNodeInfo.new_for_xml(ITEM_XML).interfaces[0]
        menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
        self.ids = [
            self.bus.register_object(ITEM_PATH, item, self._item_call, self._item_prop, None),
            self.bus.register_object(MENU_PATH, menu, self._menu_call, self._menu_prop, None),
        ]
        self.name_id = Gio.bus_own_name_on_connection(self.bus, self.service,
                                                      Gio.BusNameOwnerFlags.NONE, None, None)
        # the tray host can come and go (e.g. GNOME Shell restarting): register each time
        self.watch_id = Gio.bus_watch_name_on_connection(
            self.bus, WATCHER, Gio.BusNameWatcherFlags.NONE, self._watcher_up, self._watcher_gone)
        return True

    def stop(self):
        if not self.bus:
            return
        if self.watch_id:
            Gio.bus_unwatch_name(self.watch_id)
        if self.name_id:
            Gio.bus_unown_name(self.name_id)
        for i in self.ids:
            self.bus.unregister_object(i)
        self.ids, self.name_id, self.watch_id = [], 0, 0
        self._set_shown(False)
        self.bus = None

    def _watcher_up(self, conn, name, owner):
        def done(c, res):
            try:
                c.call_finish(res)
                self._set_shown(True)
            except GLib.Error:
                self._set_shown(False)
        conn.call(WATCHER, "/StatusNotifierWatcher", WATCHER, "RegisterStatusNotifierItem",
                  GLib.Variant("(s)", (self.service,)), None, Gio.DBusCallFlags.NONE, 3000,
                  None, done)

    def _watcher_gone(self, *_):
        self._set_shown(False)

    def _set_shown(self, v):
        if v != self.shown:
            self.shown = v
            if self.on_change:
                self.on_change(v)

    # ---- the icon
    def _item_prop(self, conn, sender, path, iface, prop):
        vals = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "tux"),
            "Title": GLib.Variant("s", self.title),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", "" if self.pixmaps else "dialog-question"),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", ("", [], self.title,
                                                    "Click to call Tux up or put him away")),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", MENU_PATH),
        }
        return vals.get(prop)

    def _item_call(self, conn, sender, path, iface, method, params, inv):
        if method in ("Activate", "SecondaryActivate"):
            x, y = params.unpack()  # where you clicked (0, 0 if the tray doesn't say)
            GLib.idle_add(lambda: (self.on_click(x, y), False)[-1])
        inv.return_value(None)

    # ---- its right-click menu (ids: 1.. in menu order)
    def _item(self, i, entry):
        if entry is None:
            props = {"type": GLib.Variant("s", "separator")}
        else:
            props = {"label": GLib.Variant("s", entry[0]), "enabled": GLib.Variant("b", True),
                     "visible": GLib.Variant("b", True)}
        return i, props

    def _layout(self):
        kids = [GLib.Variant("(ia{sv}av)", self._item(i, e) + ([],))
                for i, e in enumerate(self.menu, 1)]
        return (0, {"children-display": GLib.Variant("s", "submenu")}, kids)

    def _menu_prop(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _menu_call(self, conn, sender, path, iface, method, params, inv):
        if method == "GetLayout":
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (1, self._layout())))
        elif method == "GetGroupProperties":
            ids = params.unpack()[0]
            rows = [self._item(i, self.menu[i - 1]) for i in ids if 1 <= i <= len(self.menu)]
            inv.return_value(GLib.Variant("(a(ia{sv}))", (rows,)))
        elif method == "GetProperty":
            i, name = params.unpack()
            props = self._item(i, self.menu[i - 1])[1] if 1 <= i <= len(self.menu) else {}
            if name in props:
                inv.return_value(GLib.Variant("(v)", (props[name],)))
            else:
                inv.return_dbus_error("com.canonical.dbusmenu.Error", "no such property")
        elif method in ("Event", "EventGroup"):
            events = [params.unpack()] if method == "Event" else params.unpack()[0]
            for i, kind, _data, _ts in events:
                if kind == "clicked" and 1 <= i <= len(self.menu) and self.menu[i - 1]:
                    cb = self.menu[i - 1][1]
                    GLib.idle_add(lambda cb=cb: (cb(), False)[-1])
            inv.return_value(None if method == "Event" else GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_dbus_error("org.freedesktop.DBus.Error.UnknownMethod", method)
