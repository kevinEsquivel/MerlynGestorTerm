"""Fija las versiones de GTK antes de cualquier import de gi.repository."""
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
try:
    gi.require_version("GtkSource", "4")  # opcional: editor con resaltado
except ValueError:
    pass
