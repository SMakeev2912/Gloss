"""Палитра, шрифты и элементы окон Gloss: карточки, кнопки-пилюли, значки шагов, флажки, полоса загрузки.

Tk не сглаживает фигуры, поэтому скругления, кольца и иконки рисуются через Pillow
в увеличенном виде и уменьшаются (так края получаются гладкими). Каждая картинка
рисуется сразу на цвете фона под ней, без прозрачности.
"""
from __future__ import annotations

import sys
import tkinter as tk
import tkinter.font
from functools import lru_cache
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageTk

COLORS = {
    "bg": "#19191b",          # фон окна
    "panel": "#222225",       # карточки
    "raised": "#2b2b2f",      # кнопки, клавиши, выпуклые элементы
    "hover": "#34343a",
    "line": "#323238",        # тонкие линии и рамки
    "line_strong": "#46464e",
    "text": "#ececf0",
    "muted": "#9a9aa3",
    "faint": "#63636b",
    "accent": "#f0883e",      # акцент для текста
    "accent_from": "#f7b450",  # градиент акцента: от оранжевого…
    "accent_to": "#dd5f38",    # …к медному
    "selected": "#27231f",    # выбранная карточка: фон с тёплым оттенком
    "ok": "#3ccf6e",
    "warn": "#f5a524",
    "fail": "#ff6259",
}
GLYPHS = {"ok": "✔", "warn": "⚠", "fail": "✖"}
SS = 4  # во сколько раз крупнее рисуем перед уменьшением

_fonts_loaded = False


# --- Шрифты -----------------------------------------------------------------------

def assets_dir() -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / "assets"


def font_file(bold: bool = False) -> Path:
    return assets_dir() / "fonts" / f"JetBrainsMono-{'SemiBold' if bold else 'Regular'}.ttf"


def load_fonts() -> None:
    """Подключает встроенный JetBrains Mono (только для Gloss, в систему не ставится)."""
    global _fonts_loaded
    import winapi
    _fonts_loaded = all(winapi.load_private_font(str(font_file(b))) for b in (False, True))


def mono(size: int = 10, bold: bool = False) -> tuple:
    if _fonts_loaded:
        return ("JetBrains Mono SemiBold" if bold else "JetBrains Mono", size)
    return ("Consolas", size, "bold") if bold else ("Consolas", size)


def sans(size: int = 10, bold: bool = False) -> tuple:
    return ("Segoe UI Semibold" if bold else "Segoe UI", size)


@lru_cache(maxsize=16)
def _pil_font(size: int, bold: bool) -> ImageFont.FreeTypeFont:
    try:
        return ImageFont.truetype(str(font_file(bold)), size)
    except OSError:
        return ImageFont.load_default(size=size)


def scale_of(widget: tk.Misc) -> float:
    return widget.winfo_fpixels("1i") / 96


def bg_of(widget: tk.Misc) -> str:
    try:
        return widget.cget("bg")
    except tk.TclError:
        return COLORS["bg"]


# --- Рисование --------------------------------------------------------------------

def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


def _gradient(size: tuple[int, int], stops: tuple[str, ...]) -> Image.Image:
    """Диагональный градиент: снизу слева первый цвет, сверху справа последний."""
    w, h = size
    horizontal = Image.linear_gradient("L").rotate(90).resize((w, h))   # слева 255 → справа 0
    vertical = Image.linear_gradient("L").resize((w, h))                # сверху 0 → снизу 255
    ramp = ImageOps.invert(Image.blend(horizontal, vertical, 0.5))
    if len(stops) == 3:
        return ImageOps.colorize(ramp, black=stops[0], mid=stops[1], white=stops[2])
    return ImageOps.colorize(ramp, black=stops[0], white=stops[-1])


def accent_gradient(size: tuple[int, int]) -> Image.Image:
    return _gradient(size, (COLORS["accent_to"], COLORS["accent_from"]))


def _round_mask(size: tuple[int, int], box: tuple[float, float, float, float], radius: float) -> Image.Image:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(box, radius=max(0, radius), fill=255)
    return mask


def _finish(big: Image.Image, size: tuple[int, int]) -> Image.Image:
    return big.resize(size, Image.LANCZOS)


@lru_cache(maxsize=256)
def rounded(w: int, h: int, radius: int, fill: str, border: str | None, bg: str, border_px: float = 1.0,
            accent_border: bool = False) -> Image.Image:
    """Скруглённый прямоугольник с рамкой. accent_border – рамка градиентом, затухающим к низу слева."""
    W, H, R = w * SS, h * SS, radius * SS
    image = Image.new("RGB", (W, H), bg)
    outer = (0, 0, W - 1, H - 1)
    if border or accent_border:
        if accent_border:
            layer = _gradient((W, H), (COLORS["line"], COLORS["accent_to"], COLORS["accent_from"]))
        else:
            layer = Image.new("RGB", (W, H), border)
        image.paste(layer, mask=_round_mask((W, H), outer, R))
        inset = border_px * SS
        image.paste(Image.new("RGB", (W, H), fill),
                    mask=_round_mask((W, H), (inset, inset, W - 1 - inset, H - 1 - inset), R - inset))
    else:
        image.paste(Image.new("RGB", (W, H), fill), mask=_round_mask((W, H), outer, R))
    return _finish(image, (w, h))


def _ring_layers(d: int, width: float) -> tuple[int, Image.Image]:
    D = d * SS
    mask = Image.new("L", (D, D), 0)
    draw = ImageDraw.Draw(mask)
    draw.ellipse((0, 0, D - 1, D - 1), fill=255)
    inset = width * SS
    draw.ellipse((inset, inset, D - 1 - inset, D - 1 - inset), fill=0)
    return D, mask


@lru_cache(maxsize=128)
def step_icon(state: str, d: int, bg: str, label: str = "") -> Image.Image:
    """Круглый значок шага: pending/number, done, failed, warn, skipped (active – см. spinner_frames)."""
    D = d * SS
    image = Image.new("RGB", (D, D), bg)
    draw = ImageDraw.Draw(image)
    stroke = max(1.0, d / 14) * SS
    ring = {"done": COLORS["line_strong"], "failed": COLORS["fail"], "warn": COLORS["warn"]}.get(state, COLORS["line_strong"])
    fill = COLORS["raised"] if state in ("done", "failed", "warn", "number") else bg
    draw.ellipse((0, 0, D - 1, D - 1), fill=ring)
    draw.ellipse((stroke, stroke, D - 1 - stroke, D - 1 - stroke), fill=fill)
    c = D / 2
    line = max(1.6, d / 9) * SS
    if state == "done":
        pts = [(c - D * 0.2, c + D * 0.01), (c - D * 0.06, c + D * 0.15), (c + D * 0.21, c - D * 0.14)]
        draw.line(pts, fill=COLORS["ok"], width=int(line), joint="curve")
    elif state == "failed":
        k = D * 0.15
        draw.line((c - k, c - k, c + k, c + k), fill=COLORS["fail"], width=int(line))
        draw.line((c - k, c + k, c + k, c - k), fill=COLORS["fail"], width=int(line))
    elif state == "warn":
        draw.line((c, c - D * 0.18, c, c + D * 0.04), fill=COLORS["warn"], width=int(line))
        r = line * 0.6
        draw.ellipse((c - r, c + D * 0.16 - r, c + r, c + D * 0.16 + r), fill=COLORS["warn"])
    elif state == "skipped":
        draw.line((c - D * 0.14, c, c + D * 0.14, c), fill=COLORS["faint"], width=int(line))
    if label:
        draw.text((c, c), label, font=_pil_font(int(D * 0.46), True),
                  fill=COLORS["text"] if state == "number" else COLORS["muted"], anchor="mm")
    return _finish(image, (d, d))


@lru_cache(maxsize=16)
def spinner_frames(d: int, bg: str, count: int = 12) -> tuple[Image.Image, ...]:
    """Кольцо с бегущей градиентной дугой (как в референсе), кадры для анимации."""
    D, ring_mask = _ring_layers(d, max(1.4, d / 10))
    frames = []
    gradient = accent_gradient((D, D))
    for i in range(count):
        image = Image.new("RGB", (D, D), bg)
        image.paste(Image.new("RGB", (D, D), COLORS["line_strong"]), mask=ring_mask)
        arc = Image.new("L", (D, D), 0)
        start = i * 360 / count
        ImageDraw.Draw(arc).pieslice((0, 0, D - 1, D - 1), start, start + 120, fill=255)
        arc = Image.composite(ring_mask, Image.new("L", (D, D), 0), arc)
        image.paste(gradient, mask=arc)
        r = D * 0.17
        image.paste(gradient, mask=_ellipse_mask(D, (D / 2 - r, D / 2 - r, D / 2 + r, D / 2 + r)))
        frames.append(_finish(image, (d, d)))
    return tuple(frames)


def _ellipse_mask(D: int, box) -> Image.Image:
    mask = Image.new("L", (D, D), 0)
    ImageDraw.Draw(mask).ellipse(box, fill=255)
    return mask


@lru_cache(maxsize=64)
def indicator(kind: str, checked: bool, d: int, bg: str, enabled: bool = True) -> Image.Image:
    """Флажок (check) или переключатель (radio)."""
    D = d * SS
    image = Image.new("RGB", (D, D), bg)
    radius = D / 2 if kind == "radio" else D * 0.28
    border = COLORS["line_strong"] if enabled else COLORS["line"]
    image.paste(rounded(d, d, int(radius / SS), COLORS["raised"], border, bg, max(1.0, d / 14)).resize((D, D)))
    if checked and kind == "radio":
        r = D * 0.24
        image.paste(accent_gradient((D, D)), mask=_ellipse_mask(D, (D / 2 - r, D / 2 - r, D / 2 + r, D / 2 + r)))
    elif checked:
        image.paste(accent_gradient((D, D)), mask=_round_mask((D, D), (0, 0, D - 1, D - 1), radius))
        c = D / 2
        pts = [(c - D * 0.22, c + D * 0.01), (c - D * 0.06, c + D * 0.16), (c + D * 0.23, c - D * 0.16)]
        ImageDraw.Draw(image).line(pts, fill=COLORS["bg"], width=int(D * 0.12), joint="curve")
    return _finish(image, (d, d))


@lru_cache(maxsize=64)
def line_icon(name: str, d: int, bg: str, color: str | None = None) -> Image.Image:
    """Тонкая контурная иконка. Пока одна: замок (тексты и права остаются на компьютере)."""
    D = d * SS
    image = Image.new("RGB", (D, D), bg)
    draw = ImageDraw.Draw(image)
    col = color or COLORS["muted"]
    w = int(max(1.3, d / 13) * SS)
    u = D / 24  # сетка 24×24, как у обычных наборов иконок
    if name == "lock":
        draw.rounded_rectangle((5 * u, 10.5 * u, 19 * u, 21 * u), radius=2.5 * u, outline=col, width=w)
        draw.arc((8 * u, 3 * u, 16 * u, 13.5 * u), 180, 360, fill=col, width=w)
        draw.line(((8 * u, 8.2 * u), (8 * u, 10.5 * u)), fill=col, width=w)
        draw.line(((16 * u, 8.2 * u), (16 * u, 10.5 * u)), fill=col, width=w)
    return _finish(image, (d, d))


def photo(image: Image.Image, master: tk.Misc) -> ImageTk.PhotoImage:
    return ImageTk.PhotoImage(image, master=master)


# --- Элементы ---------------------------------------------------------------------

class Panel(tk.Frame):
    """Карточка со скруглёнными углами. Содержимое кладётся в .inner.

    Скруглённая подложка – картинка под содержимым; отступ внутри не меньше трети радиуса,
    поэтому прямоугольник с содержимым не заходит на скруглённые углы.
    """

    def __init__(self, parent: tk.Misc, *, fill: str | None = None, border: str | None = "line", radius: int = 14,
                 padding: tuple[int, int] = (16, 12), accent: bool = False):
        self.outer_bg = bg_of(parent)
        super().__init__(parent, bg=self.outer_bg, bd=0, highlightthickness=0)
        self.s = scale_of(parent)
        self.fill = fill or COLORS["panel"]
        self.border = COLORS[border] if border in COLORS else border
        self.radius = int(radius * self.s)
        self.accent = accent
        self._backdrop = tk.Label(self, bg=self.outer_bg, bd=0)
        self._backdrop.place(x=0, y=0, relwidth=1, relheight=1)
        self.inner = tk.Frame(self, bg=self.fill)
        self.inner.pack(fill="both", expand=True, padx=int(padding[0] * self.s), pady=int(padding[1] * self.s))
        self._photo = None
        self._drawn = None
        self.bind("<Configure>", self._redraw, add="+")

    def _redraw(self, _event=None) -> None:
        w, h = self.winfo_width(), self.winfo_height()
        if w < 4 or h < 4:
            return
        key = (w, h, self.fill, self.border, self.accent)
        if key == self._drawn:
            return
        self._drawn = key
        width = max(1.0, self.s) * (1.4 if self.accent else 1)
        self._photo = photo(rounded(w, h, self.radius, self.fill, self.border, self.outer_bg, width, self.accent), self)
        self._backdrop.configure(image=self._photo)

    def set_style(self, fill: str | None = None, accent: bool | None = None) -> None:
        if fill:
            self.fill = fill
            _recolor(self.inner, self.fill)
        if accent is not None:
            self.accent = accent
        self._redraw()


def _recolor(widget: tk.Misc, color: str) -> None:
    """Меняет фон у рамки и всех вложенных меток (у которых фон совпадал с прежним)."""
    old = bg_of(widget)
    stack = [widget]
    while stack:
        item = stack.pop()
        try:
            if item.cget("bg") == old:
                item.configure(bg=color)
        except tk.TclError:
            pass
        stack.extend(item.winfo_children())


class PillButton(tk.Label):
    """Кнопка-пилюля. Основная (primary) с градиентным контуром, второстепенная графитовая."""

    def __init__(self, parent: tk.Misc, text: str = "", command: Callable | None = None, kind: str = "secondary",
                 min_width: int = 132, height: int = 36, font: tuple | None = None):
        self.bg = bg_of(parent)
        super().__init__(parent, bg=self.bg, bd=0, padx=0, pady=0, highlightthickness=0, compound="center",
                         cursor="hand2")
        self.s = scale_of(parent)
        self.kind = kind
        self.command = command
        self.state = "normal"
        self.min_width = int(min_width * self.s)
        self.height = int(height * self.s)
        self.text_font = font or mono(10, bold=kind == "primary")
        self._hover = False
        self._pressed = False
        self._text = text
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self._render()

    def _visual_state(self) -> str:
        if self.state == "disabled":
            return "disabled"
        return "pressed" if self._pressed else "hover" if self._hover else "normal"

    def _render(self) -> None:
        width = max(self.min_width, tk.font.Font(font=self.text_font).measure(self._text) + int(44 * self.s))
        state = self._visual_state()
        primary = self.kind == "primary" and state != "disabled"
        fill = {"normal": COLORS["raised"], "hover": COLORS["hover"], "pressed": COLORS["panel"],
                "disabled": COLORS["panel"]}[state]
        image = rounded(width, self.height, self.height // 2, fill, COLORS["line_strong"] if not primary else None,
                        self.bg, max(1.0, self.s) * (1.5 if primary else 1), primary)
        self._photo = photo(image, self)
        fg = COLORS["faint"] if state == "disabled" else COLORS["text"]
        super().configure(image=self._photo, text=self._text, fg=fg, font=self.text_font,
                          cursor="arrow" if state == "disabled" else "hand2")

    def _set_hover(self, value: bool) -> None:
        self._hover = value
        if not value:
            self._pressed = False
        self._render()

    def _press(self, _event) -> None:
        if self.state != "disabled":
            self._pressed = True
            self._render()

    def _release(self, event) -> None:
        was_pressed, self._pressed = self._pressed, False
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        self._render()
        if was_pressed and inside:
            self.invoke()

    def invoke(self) -> None:
        if self.state != "disabled" and self.command:
            self.command()

    def instate(self, states: list[str]) -> bool:
        return all((self.state == "disabled") == (s == "disabled") for s in states)

    def configure(self, cnf=None, **kw):
        changed = False
        for key, attr in (("text", "_text"), ("command", "command"), ("state", "state")):
            if key in kw:
                setattr(self, attr, kw.pop(key))
                changed = True
        result = super().configure(cnf, **kw) if (cnf or kw) else None
        if changed:
            self._render()
        return result

    config = configure


class Check(tk.Frame):
    """Флажок или переключатель: свой значок и подпись, щелчок по подписи тоже работает."""

    def __init__(self, parent: tk.Misc, text: str, variable: tk.Variable, value=None, kind: str = "check",
                 command: Callable | None = None, font: tuple | None = None, wrap: int = 0, enabled: bool = True):
        bg = bg_of(parent)
        super().__init__(parent, bg=bg)
        self.s = scale_of(parent)
        self.kind, self.variable, self.value, self.command, self.enabled = kind, variable, value, command, enabled
        self.box = tk.Label(self, bg=bg, bd=0)
        self.box.pack(side="left", anchor="n", pady=(int(2 * self.s), 0))
        self.label = tk.Label(self, text=text, bg=bg, fg=COLORS["text"] if enabled else COLORS["faint"],
                              font=font or sans(10), anchor="w", justify="left",
                              wraplength=int(wrap * self.s) if wrap else 0)
        self.label.pack(side="left", padx=(int(9 * self.s), 0), fill="x", expand=True)
        for widget in (self, self.box, self.label):
            widget.bind("<Button-1>", self._click)
            if enabled:
                widget.configure(cursor="hand2")
        self._trace = variable.trace_add("write", lambda *_: self._render())
        self.bind("<Destroy>", lambda e: self._untrace() if e.widget is self else None)
        self._render()

    def _untrace(self) -> None:
        try:
            self.variable.trace_remove("write", self._trace)
        except tk.TclError:
            pass

    def _checked(self) -> bool:
        return bool(self.variable.get()) if self.kind == "check" else self.variable.get() == self.value

    def _render(self) -> None:
        if not self.winfo_exists():
            return
        image = indicator(self.kind, self._checked(), int(18 * self.s), bg_of(self), self.enabled)
        self._photo = photo(image, self)
        self.box.configure(image=self._photo)

    def _click(self, _event) -> None:
        if not self.enabled:
            return
        if self.kind == "check":
            self.variable.set(not self.variable.get())
        else:
            self.variable.set(self.value)
        if self.command:
            self.command()


class ProgressBar(tk.Canvas):
    """Полоса загрузки-пилюля с градиентом. set(None) – бегущий отрезок, когда доля неизвестна."""

    def __init__(self, parent: tk.Misc, height: int = 8):
        self.bg = bg_of(parent)
        self.s = scale_of(parent)
        super().__init__(parent, bg=self.bg, highlightthickness=0, bd=0, height=int(height * self.s))
        self.h = int(height * self.s)
        self.fraction: float | None = 0.0
        self._phase = 0.0
        self._job = None
        self._track = self.create_image(0, 0, anchor="nw")
        self._fill = self.create_image(0, 0, anchor="nw")
        self._photos = {}
        self.bind("<Configure>", lambda e: self._draw())
        self.bind("<Destroy>", lambda e: self._stop_anim() if e.widget is self else None)

    def set(self, fraction: float | None) -> None:
        self.fraction = None if fraction is None else max(0.0, min(1.0, fraction))
        if self.fraction is None and self._job is None:
            self._animate()
        elif self.fraction is not None:
            self._stop_anim()
        self._draw()

    def _stop_anim(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None

    def _animate(self) -> None:
        self._phase = (self._phase + 0.018) % 1.4
        self._draw()
        self._job = self.after(30, self._animate)

    def _image(self, key, factory):
        if key not in self._photos:
            if len(self._photos) > 40:
                self._photos.clear()
            self._photos[key] = photo(factory(), self)
        return self._photos[key]

    def _draw(self) -> None:
        w = self.winfo_width()
        if w < 4:
            return
        h = self.h
        self.itemconfigure(self._track, image=self._image(("track", w), lambda: rounded(w, h, h // 2, COLORS["raised"],
                                                                                         None, self.bg)))
        if self.fraction is None:
            seg = max(h, int(w * 0.3))
            x = int((self._phase - 0.3) * w)
            self.itemconfigure(self._fill, image=self._image(("seg", seg), lambda: accent_pill(seg, h, self.bg)))
            self.coords(self._fill, x, 0)
        elif self.fraction <= 0:
            self.itemconfigure(self._fill, image="")
        else:
            fw = max(h, int(w * self.fraction))
            self.itemconfigure(self._fill, image=self._image(("fill", fw), lambda: accent_pill(fw, h, self.bg)))
            self.coords(self._fill, 0, 0)


def accent_pill(w: int, h: int, bg: str) -> Image.Image:
    W, H = w * SS, h * SS
    image = Image.new("RGB", (W, H), COLORS["raised"])
    image.paste(_gradient((W, H), (COLORS["accent_to"], COLORS["accent_from"])),
                mask=_round_mask((W, H), (0, 0, W - 1, H - 1), H / 2))
    return _finish(image, (w, h))


class StepList(tk.Frame):
    """Вертикальный список шагов: круглые значки, соединённые тонкой линией."""

    def __init__(self, parent: tk.Misc, icon: int = 22, gap: int = 12, spacing: int = 10):
        bg = bg_of(parent)
        super().__init__(parent, bg=bg)
        self.s = scale_of(parent)
        self.d = int(icon * self.s)
        self.gap = int(gap * self.s)
        self.spacing = int(spacing * self.s)
        self.rows: list[dict] = []
        self._frame = 0
        self._job = None
        self.columnconfigure(1, weight=1)
        self.bind("<Destroy>", lambda e: self._stop() if e.widget is self else None)

    def add(self, state: str = "pending", label: str = "") -> tk.Frame:
        """Добавляет строку и возвращает рамку для её содержимого."""
        index = len(self.rows)
        bg = bg_of(self)
        canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0, width=self.d, height=self.d)
        canvas.grid(row=index, column=0, sticky="ns", padx=(0, self.gap))
        content = tk.Frame(self, bg=bg)
        content.grid(row=index, column=1, sticky="ew", pady=(0, self.spacing))
        row = {"canvas": canvas, "content": content, "state": state, "label": label, "photo": None}
        self.rows.append(row)
        canvas.bind("<Configure>", lambda e, r=row: self._draw(r))
        if len(self.rows) > 1:
            self._draw(self.rows[-2])
        return content

    def set_state(self, index: int, state: str) -> None:
        row = self.rows[index]
        row["state"] = state
        self._draw(row)
        if state == "active" and self._job is None:
            self._spin()

    def _draw(self, row: dict) -> None:
        canvas, d = row["canvas"], self.d
        canvas.delete("all")
        h = max(canvas.winfo_height(), d)
        x = d / 2
        last = row is self.rows[-1]
        if not last:
            canvas.create_line(x, d + 3 * self.s, x, h + 1, fill=COLORS["line_strong"], width=max(1, int(self.s)))
        bg = bg_of(self)
        if row["state"] == "active":
            image = spinner_frames(d, bg)[self._frame % 12]
        else:
            image = step_icon(row["state"], d, bg, row["label"] if row["state"] in ("pending", "number") else "")
        row["photo"] = photo(image, self)
        canvas.create_image(0, 0, image=row["photo"], anchor="nw")

    def _spin(self) -> None:
        active = [r for r in self.rows if r["state"] == "active"]
        if not active or not self.winfo_exists():
            self._job = None
            return
        self._frame += 1
        for row in active:
            self._draw(row)
        self._job = self.after(80, self._spin)

    def _stop(self) -> None:
        if self._job is not None:
            self.after_cancel(self._job)
            self._job = None


def tag(parent: tk.Misc, text: str, accent: bool = False) -> tk.Label:
    """Маленькая пилюля-метка, например «рекомендуется»."""
    bg = bg_of(parent)
    s = scale_of(parent)
    font = mono(8, bold=accent)
    w = tk.font.Font(font=font).measure(text) + int(18 * s)
    h = int(20 * s)
    if accent:
        W, H = w * SS, h * SS
        big = Image.new("RGB", (W, H), bg)
        big.paste(accent_gradient((W, H)), mask=_round_mask((W, H), (0, 0, W - 1, H - 1), H / 2))
        image = _finish(big, (w, h))
    else:
        image = rounded(w, h, h // 2, COLORS["raised"], COLORS["line_strong"], bg)
    label = tk.Label(parent, bg=bg, bd=0, compound="center", text=text, font=font,
                     fg=COLORS["bg"] if accent else COLORS["muted"])
    label.photo = photo(image, label)
    label.configure(image=label.photo)
    return label


def keycaps(parent: tk.Misc, hotkey: str) -> tk.Frame:
    """Сочетание клавиш в виде отдельных клавиш-пилюль: Ctrl Alt G."""
    bg = bg_of(parent)
    s = scale_of(parent)
    frame = tk.Frame(parent, bg=bg)
    font = mono(10, bold=True)
    for key in hotkey.split("+"):
        w = max(int(30 * s), tk.font.Font(font=font).measure(key) + int(20 * s))
        h = int(28 * s)
        image = rounded(w, h, int(8 * s), COLORS["raised"], COLORS["line_strong"], bg)
        cap = tk.Label(frame, bg=bg, bd=0, compound="center", text=key, font=font, fg=COLORS["text"])
        cap.photo = photo(image, cap)
        cap.configure(image=cap.photo)
        cap.pack(side="left", padx=(0, int(5 * s)))
    return frame


def hotkey_table(parent: tk.Misc, rows: list[tuple[str, str]]) -> tk.Frame:
    """Сочетания клавиш и что они делают: клавиши в одной колонке, подписи выровнены."""
    bg = bg_of(parent)
    s = scale_of(parent)
    frame = tk.Frame(parent, bg=bg)
    for index, (hotkey, name) in enumerate(rows):
        keycaps(frame, hotkey).grid(row=index, column=0, sticky="w", pady=int(4 * s))
        text(frame, name, "text", sans(10)).grid(row=index, column=1, sticky="w", padx=(int(12 * s), 0))
    return frame


def icon_label(parent: tk.Misc, name: str, size: int = 18, color: str | None = None) -> tk.Label:
    bg = bg_of(parent)
    label = tk.Label(parent, bg=bg, bd=0)
    label.photo = photo(line_icon(name, int(size * scale_of(parent)), bg, color), label)
    label.configure(image=label.photo)
    return label


def status_icon(parent: tk.Misc, status: str, size: int = 18) -> tk.Label:
    """Маленький круглый значок статуса: ok/warn/fail."""
    bg = bg_of(parent)
    state = {"ok": "done", "warn": "warn", "fail": "failed"}.get(status, "skipped")
    label = tk.Label(parent, bg=bg, bd=0)
    label.photo = photo(step_icon(state, int(size * scale_of(parent)), bg), label)
    label.configure(image=label.photo)
    return label


def text(parent: tk.Misc, value: str, color: str = "text", font: tuple | None = None, wrap: int = 0,
         **kw) -> tk.Label:
    """Метка на фоне родителя.

    wrap – наибольшая ширина строки в точках при 100 % масштабе. Если метке досталось меньше места,
    перенос сужается под фактическую ширину, чтобы текст не обрезался. Такую метку нужно растягивать
    (fill="x" или sticky="ew"): иначе, сузившись, она уже не получит место обратно.
    """
    limit = int(wrap * scale_of(parent)) if wrap else 0
    label = tk.Label(parent, text=value, bg=bg_of(parent), fg=COLORS.get(color, color), font=font or sans(10),
                     anchor="w", justify="left", wraplength=limit, **kw)
    if wrap:
        def fit(event) -> None:
            target = min(limit, event.width)
            if event.width > 20 and abs(int(label.cget("wraplength")) - target) > 2:
                label.configure(wraplength=target)
        label.bind("<Configure>", fit, add="+")
    return label
