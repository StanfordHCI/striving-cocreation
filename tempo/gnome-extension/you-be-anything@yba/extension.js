/**
 * Tempo GNOME Shell Extension
 * 
 * Provides D-Bus services for Tempo:
 * - GetGlobalPointerPosition: Returns current mouse coordinates
 * - SendNotification: Displays a desktop notification
 */

import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const DBUS_INTERFACE = `
<node>
  <interface name="org.yba.GnomeSession">
    <method name="GetGlobalPointerPosition">
      <arg type="i" direction="out" name="x"/>
      <arg type="i" direction="out" name="y"/>
    </method>
    <method name="SendNotification">
      <arg type="s" direction="in" name="title"/>
      <arg type="s" direction="in" name="message"/>
    </method>
  </interface>
</node>
`;

class Extension {
    constructor() {
        this._dbusId = null;
    }

    enable() {
        this._dbusId = Gio.DBus.session.own_name(
            'org.yba.GnomeSession',
            Gio.BusNameOwnerFlags.NONE,
            this._onBusAcquired.bind(this),
            null,
            null
        );
    }

    disable() {
        if (this._dbusId) {
            Gio.DBus.session.unown_name(this._dbusId);
            this._dbusId = null;
        }
    }

    _onBusAcquired(connection, name) {
        const dbusImpl = Gio.DBusExportedObject.wrapJSObject(DBUS_INTERFACE, {
            GetGlobalPointerPosition: () => {
                const [x, y] = global.get_pointer();
                return [x, y];
            },
            SendNotification: (title, message) => {
                Main.notify(title, message);
            }
        });
        dbusImpl.export(connection, '/org/yba/GnomeSession');
    }
}

export default class YBAExtension {
    constructor() {
        this._extension = new Extension();
    }

    enable() {
        this._extension.enable();
    }

    disable() {
        this._extension.disable();
    }
}

