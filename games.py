"""Juego original de MerlynGestorTerm: «Cortafuegos».

Los paquetes viajan por 4 carriles hacia tu servidor. La regla de arriba dice
qué puertos están prohibidos (cambia cada cierto tiempo). Mueve el cortafuegos
con ↑/↓ y pulsa Espacio para bloquear el paquete más cercano de tu carril.
Bloquear un paquete prohibido suma puntos; dejar pasar uno prohibido o
bloquear uno legítimo te quita una vida."""
import math
import random

import gtkver  # noqa: F401
from gi.repository import Gdk, GLib, Gtk

PORTS = [21, 22, 23, 25, 53, 80, 110, 139, 443, 445, 1433, 3306, 3389, 5900, 8080]
LANES = 4


class FirewallGame(Gtk.Window):
    def __init__(self, app):
        super().__init__(title="Cortafuegos — MerlynGestorTerm", transient_for=app.window)
        self.set_default_size(900, 520)
        self.area = Gtk.DrawingArea(can_focus=True)
        self.area.connect("draw", self.draw)
        self.add(self.area)
        self.connect("key-press-event", self.on_key)
        self.reset()
        self.timer = GLib.timeout_add(16, self.tick)
        self.connect("destroy", lambda *_: GLib.source_remove(self.timer))
        self.show_all()

    def reset(self):
        self.lane = 1
        self.packets = []      # [carril, x (0..1), puerto, velocidad]
        self.score = 0
        self.lives = 5
        self.level = 1
        self.frame = 0
        self.flash = []        # efectos [x, y, color, vida]
        self.over = False
        self.new_rule()

    def new_rule(self):
        self.blocked = set(random.sample(PORTS, 3 + min(self.level, 4)))
        self.rule_frames = 60 * 20

    def tick(self):
        if self.over:
            self.area.queue_draw()
            return True
        self.frame += 1
        self.rule_frames -= 1
        if self.rule_frames <= 0:
            self.level += 1
            self.new_rule()
        rate = 0.012 + self.level * 0.004
        if random.random() < rate:
            self.packets.append([random.randrange(LANES), 0.0, random.choice(PORTS),
                                 0.0022 + self.level * 0.0005 + random.random() * 0.001])
        for p in self.packets:
            p[1] += p[3]
        arrived = [p for p in self.packets if p[1] >= 0.92]
        self.packets = [p for p in self.packets if p[1] < 0.92]
        for p in arrived:
            if p[2] in self.blocked:
                self.lives -= 1
                self.effect(p, (1, 0.3, 0.3))
            else:
                self.score += 1
        if self.lives <= 0:
            self.over = True
        self.flash = [[x, y, c, t - 1] for x, y, c, t in self.flash if t > 1]
        self.area.queue_draw()
        return True

    def effect(self, p, color):
        self.flash.append([p[1], p[0], color, 20])

    def on_key(self, _w, ev):
        k = ev.keyval
        if self.over and k in (Gdk.KEY_space, Gdk.KEY_Return):
            self.reset()
        elif k in (Gdk.KEY_Up, Gdk.KEY_w):
            self.lane = max(0, self.lane - 1)
        elif k in (Gdk.KEY_Down, Gdk.KEY_s):
            self.lane = min(LANES - 1, self.lane + 1)
        elif k == Gdk.KEY_space:
            lane = [p for p in self.packets if p[0] == self.lane and p[1] < 0.8]
            if lane:
                p = max(lane, key=lambda q: q[1])
                self.packets.remove(p)
                if p[2] in self.blocked:
                    self.score += 3
                    self.effect(p, (0.3, 1, 0.5))
                else:
                    self.lives -= 1
                    self.effect(p, (1, 0.7, 0.2))
                    if self.lives <= 0:
                        self.over = True
        elif k == Gdk.KEY_Escape:
            self.destroy()
        return True

    def draw(self, area, cr):
        w, h = area.get_allocated_width(), area.get_allocated_height()
        top = 70
        lane_h = (h - top - 20) / LANES
        cr.set_source_rgb(0.07, 0.09, 0.14)
        cr.paint()
        cr.select_font_face("Monospace")
        # cabecera
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(18)
        cr.move_to(16, 28)
        cr.show_text(f"Puntos {self.score}   Vidas {'♥' * self.lives}   Nivel {self.level}")
        cr.set_font_size(15)
        cr.set_source_rgb(1, 0.55, 0.55)
        cr.move_to(16, 54)
        cr.show_text("BLOQUEAR puertos: " + ", ".join(str(p) for p in sorted(self.blocked)) +
                     f"   (nueva regla en {self.rule_frames // 60}s)")
        # carriles
        for i in range(LANES):
            y = top + i * lane_h
            cr.set_source_rgba(1, 1, 1, 0.05 if i % 2 else 0.08)
            cr.rectangle(0, y, w, lane_h)
            cr.fill()
        # servidor
        cr.set_source_rgb(0.3, 0.5, 0.9)
        cr.rectangle(w * 0.93, top, w * 0.07, h - top - 20)
        cr.fill()
        cr.set_source_rgb(1, 1, 1)
        cr.set_font_size(12)
        cr.move_to(w * 0.935, top + 20)
        cr.show_text("SERVIDOR")
        # cortafuegos
        fx = w * 0.8
        y = top + self.lane * lane_h
        cr.set_source_rgb(1, 0.6, 0.1)
        cr.rectangle(fx, y + 6, 14, lane_h - 12)
        cr.fill()
        # paquetes
        cr.set_font_size(14)
        for lane, x, port, _v in self.packets:
            px, py = x * w, top + lane * lane_h + lane_h / 2
            # a partir del nivel 3 los colores ya no delatan los paquetes prohibidos
            danger = port in self.blocked and self.level < 3
            cr.set_source_rgb(*((0.85, 0.25, 0.3) if danger else (0.3, 0.55, 0.75) if self.level >= 3
                                else (0.25, 0.7, 0.45)))
            cr.rectangle(px - 28, py - 14, 56, 28)
            cr.fill()
            cr.set_source_rgb(1, 1, 1)
            ext = cr.text_extents(str(port))
            cr.move_to(px - ext.width / 2, py + ext.height / 2)
            cr.show_text(str(port))
        for x, lane, color, t in self.flash:
            cr.set_source_rgba(*color, t / 20)
            cr.arc(x * w, top + lane * lane_h + lane_h / 2, 40 - t, 0, 2 * math.pi)
            cr.stroke()
        if self.over:
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.paint()
            cr.set_source_rgb(1, 1, 1)
            cr.set_font_size(32)
            cr.move_to(w / 2 - 170, h / 2)
            cr.show_text(f"Red comprometida · {self.score} pts")
            cr.set_font_size(16)
            cr.move_to(w / 2 - 150, h / 2 + 34)
            cr.show_text("Espacio para jugar otra vez · Esc para salir")
        else:
            cr.set_source_rgba(1, 1, 1, 0.4)
            cr.set_font_size(12)
            cr.move_to(16, h - 6)
            cr.show_text("↑/↓ mover · Espacio bloquear · rojo = prohibido (desde el nivel 3, lee la regla)")
