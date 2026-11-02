// Tux Perch: GNOME doesn't let apps keep themselves on top or pick their position
// on Wayland, so this tiny extension does it for the Tux window only.
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const TUX_ID = 'local.tux.helper';
const MARGIN = 24;  // px from the right edge of the screen

export default class TuxPerchExtension extends Extension {
    enable() {
        this._handles = [];        // [object, signalId] to disconnect on disable
        this._windows = new Set();
        this._dragging = new Set();
        this._connect(global.display, 'window-created', (_d, w) => this._watch(w));
        this._connect(global.display, 'grab-op-begin', (_d, w) => w && this._dragging.add(w));
        this._connect(global.display, 'grab-op-end', (_d, w) => w && this._dragging.delete(w));
        for (const actor of global.get_window_actors())
            this._watch(actor.meta_window, true);
    }

    disable() {
        for (const [obj, id] of this._handles) {
            try {
                obj.disconnect(id);
            } catch (e) {}
        }
        for (const w of this._windows) {
            try {
                w.unmake_above();
                w.unstick();
            } catch (e) {}
        }
        this._handles = [];
        this._windows.clear();
        this._dragging.clear();
    }

    _connect(obj, signal, fn) {
        const id = obj.connect(signal, fn);
        this._handles.push([obj, id]);
        return id;
    }

    _isTux(w) {
        return w.get_wm_class() === TUX_ID || w.get_gtk_application_id() === TUX_ID;
    }

    _watch(w, existing = false) {
        if (!w)
            return;
        if (existing) {
            if (this._isTux(w))
                this._perch(w, false);
            return;
        }
        // the app id is only known once the window shows
        const id = w.connect('shown', () => {
            w.disconnect(id);
            this._handles = this._handles.filter(h => h[1] !== id);
            if (this._isTux(w))
                this._perch(w, true);
        });
        this._handles.push([w, id]);
    }

    _perch(w, place) {
        this._windows.add(w);
        w.make_above();
        w.stick();  // on every workspace
        if (place) {
            // bottom-right, feet on the dock (the work area stops at the dock)
            GLib.idle_add(GLib.PRIORITY_DEFAULT, () => {
                const wa = w.get_work_area_for_monitor(Main.layoutManager.primaryIndex);
                const r = w.get_frame_rect();
                w.move_frame(true, wa.x + wa.width - r.width - MARGIN, wa.y + wa.height - r.height);
                return GLib.SOURCE_REMOVE;
            });
        }
        // Ctrl+scroll resize: keep his feet planted (anchor bottom-right).
        // Dragging the resize grip is left alone.
        let last = w.get_frame_rect();
        this._connect(w, 'size-changed', () => {
            const r = w.get_frame_rect();
            if (!this._dragging.has(w) && last.width > 0 &&
                (r.width !== last.width || r.height !== last.height)) {
                w.move_frame(true, last.x + last.width - r.width, last.y + last.height - r.height);
            }
            last = w.get_frame_rect();
        });
        this._connect(w, 'position-changed', () => {
            last = w.get_frame_rect();
        });
        this._connect(w, 'unmanaged', () => {
            this._windows.delete(w);
            this._dragging.delete(w);
        });
    }
}
