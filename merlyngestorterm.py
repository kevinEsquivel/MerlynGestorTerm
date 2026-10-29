#!/usr/bin/env python3
"""MerlynGestorTerm - terminal portable con sesiones remotas, explorador SFTP,
servidores y herramientas de red para Linux (GTK3 + VTE)."""
import os
import sys

import core

if len(sys.argv) > 1 and sys.argv[1] == "--askpass":
    sys.exit(core.askpass_main(" ".join(sys.argv[2:])))

from gi.repository import Gio, Gtk  # noqa: E402

from window import MainWindow  # noqa: E402


class App(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="io.github.merlyngestorterm.MerlynGestorTerm", flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        MainWindow(self).present()


if __name__ == "__main__":
    os.makedirs(core.DATA_DIR, exist_ok=True)
    sys.exit(App().run(sys.argv[:1]))
