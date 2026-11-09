import base64
from concurrent.futures import ThreadPoolExecutor
from email.utils import parsedate_to_datetime
import json
import math
import os
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
import time
import tkinter as tk
from tkinter import ttk
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import webbrowser


TILE_SIZE = 256
MAX_LATITUDE = 85.05112878
CACHE_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PikminRoute" / "map-tiles"


def project(latitude, longitude):
    latitude = math.radians(max(-MAX_LATITUDE, min(MAX_LATITUDE, latitude)))
    return (longitude + 180) / 360, (1 - math.asinh(math.tan(latitude)) / math.pi) / 2


def unproject(x, y):
    latitude = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * max(0, min(1, y))))))
    return latitude, (x % 1) * 360 - 180


def load_tile(zoom, x, y):
    cache_file = CACHE_DIR / str(zoom) / str(x) / f"{y}.json"
    cached = {}
    try:
        cached = json.loads(cache_file.read_text(encoding="ascii"))
    except (OSError, ValueError):
        pass
    now = time.time()
    if cached.get("expires", 0) > now:
        return cached["png"]

    headers = {"User-Agent": "PikminRoute/0.3 (Windows route map)"}
    for key, header in (("etag", "If-None-Match"), ("modified", "If-Modified-Since")):
        if cached.get(key):
            headers[header] = cached[key]
    request = Request(f"https://tile.openstreetmap.org/{zoom}/{x}/{y}.png", headers=headers)
    try:
        with urlopen(request, timeout=6) as response:
            response_headers = response.headers
            png = base64.b64encode(response.read()).decode("ascii")
    except HTTPError as error:
        with error:
            if error.code != 304 or "png" not in cached:
                raise
            response_headers = error.headers
            png = cached["png"]

    cache_control = response_headers.get("Cache-Control", "")
    max_age = re.search(r"(?:^|,\s*)max-age=(\d+)", cache_control)
    expires = response_headers.get("Expires")
    if max_age:
        lifetime = max(0, int(max_age[1]) - int(response_headers.get("Age", "0")))
        expires_at = now + lifetime
    elif expires:
        expires_at = parsedate_to_datetime(expires).timestamp()
    else:
        expires_at = now + 7 * 86400
    record = {
        "expires": expires_at,
        "etag": response_headers.get("ETag", cached.get("etag")),
        "modified": response_headers.get("Last-Modified", cached.get("modified")),
        "png": png,
    }
    if "no-store" not in cache_control:
        temporary = None
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="ascii", dir=cache_file.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(record, stream)
            temporary.replace(cache_file)
        except OSError:
            pass
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return png


class RouteMap(tk.Toplevel):
    def __init__(self, parent, points, on_edit, *, single=False):
        super().__init__(parent)
        self.title("選擇停留定點" if single else "PikminRoute 路線地圖")
        self.geometry("820x580")
        self.minsize(480, 330)
        self.points = []
        self.center = project(23.7, 120.95)
        self.zoom = 8
        self.press = None
        self.selected = None
        self.insert_at = None
        self.images = {}
        self.pending = {}
        self.failed = set()
        self.workers = ThreadPoolExecutor(max_workers=4)
        self.on_edit = on_edit
        self.single = single

        toolbar = ttk.Frame(self, padding=6)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="+", width=4, command=lambda: self.change_zoom(1)).pack(side="left")
        ttk.Button(toolbar, text="−", width=4, command=lambda: self.change_zoom(-1)).pack(side="left")
        ttk.Button(toolbar, text="顯示全部路標", command=lambda: self.set_points(self.points, fit=True)).pack(
            side="left", padx=6
        )
        if not single:
            self.insert_button = ttk.Button(toolbar, text="在選取點後插入", command=self.prepare_insert)
            self.insert_button.pack(side="left", padx=(0, 6))
            self.delete_button = ttk.Button(toolbar, text="刪除選取點", command=self.delete_point)
            self.delete_button.pack(side="left")
        help_text = "點一下選定位置 · 拖曳底圖移動 · 滾輪縮放" if single else (
            "點空白處新增 · 點路標選取 · 拖曳路標移動 · 拖曳底圖移動"
        )
        ttk.Label(self, text=help_text, padding=(6, 0, 6, 5)).pack(anchor="w")
        self.canvas = tk.Canvas(self, background="#e6ebee", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        footer = ttk.Frame(self, padding=4)
        footer.pack(fill="x")
        self.status = tk.StringVar(self)
        ttk.Label(footer, textvariable=self.status).pack(side="left")
        attribution = ttk.Label(footer, text="© OpenStreetMap contributors", cursor="hand2")
        attribution.pack(side="right")
        attribution.bind("<Button-1>", lambda _: webbrowser.open("https://www.openstreetmap.org/copyright"))
        self.canvas.bind("<Configure>", lambda _: self.draw())
        self.canvas.bind("<ButtonPress-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.finish_drag)
        self.canvas.bind("<MouseWheel>", lambda event: self.change_zoom(1 if event.delta > 0 else -1, event))
        self.bind("<Escape>", self.cancel_insert)
        self.poll_id = self.after(75, self.poll_tiles)
        self.set_points(points, fit=True)

    def destroy(self):
        self.after_cancel(self.poll_id)
        self.workers.shutdown(wait=False, cancel_futures=True)
        super().destroy()

    def set_points(self, points, fit=False):
        if self.points != points:
            self.selected = None
            self.insert_at = None
        self.points = list(points)
        if fit and points:
            projected = self.projected_route(points)
            xs, ys = zip(*projected)
            self.center = ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)
            width = max(400, self.canvas.winfo_width() - 100)
            height = max(250, self.canvas.winfo_height() - 100)
            span = max((max(xs) - min(xs)) / width, (max(ys) - min(ys)) / height)
            self.zoom = 16 if span == 0 else max(2, min(17, math.floor(math.log2(1 / (TILE_SIZE * span)))))
        self.draw(fetch=self.canvas.winfo_ismapped())

    def projected_route(self, points):
        reference = project(*points[0])[0]
        projected = []
        for point in points:
            x, y = project(*point)
            x += round(reference - x)
            projected.append((x, y))
            reference = x
        return projected

    def origin(self):
        scale = TILE_SIZE << self.zoom
        return (
            scale,
            self.center[0] * scale - self.canvas.winfo_width() / 2,
            self.center[1] * scale - self.canvas.winfo_height() / 2,
        )

    def point_positions(self):
        if not self.points:
            return []
        scale, left, top = self.origin()
        projected = self.projected_route(self.points)
        offset = round(self.center[0] - projected[0][0])
        return [((x + offset) * scale - left, y * scale - top) for x, y in projected]

    def draw(self, fetch=True):
        scale, left, top = self.origin()
        width, height = self.canvas.winfo_width(), self.canvas.winfo_height()
        tile_count = 1 << self.zoom
        placements = []
        for tile_y in range(max(0, math.floor(top / TILE_SIZE)), min(tile_count, math.ceil((top + height) / TILE_SIZE))):
            for tile_x in range(math.floor(left / TILE_SIZE), math.ceil((left + width) / TILE_SIZE)):
                key = (self.zoom, tile_x % tile_count, tile_y)
                placements.append((key, tile_x * TILE_SIZE - left, tile_y * TILE_SIZE - top))
        visible = {key for key, _, _ in placements}
        if fetch:
            for key, future in list(self.pending.items()):
                if key not in visible and future.cancel():
                    del self.pending[key]
            for key in visible - self.images.keys() - self.pending.keys() - self.failed:
                self.pending[key] = self.workers.submit(load_tile, *key)
        self.images = {key: image for key, image in self.images.items() if key in visible}
        self.canvas.delete("all")
        for key, x, y in placements:
            if key in self.images:
                self.canvas.create_image(x, y, anchor="nw", image=self.images[key])
        positions = self.point_positions()
        if len(positions) > 1:
            self.canvas.create_line(*positions, fill="#2563eb", width=4)
        for index, (x, y) in enumerate(positions):
            color = "#15803d" if index == 0 else "#dc2626" if index == len(positions) - 1 else "#2563eb"
            radius = max(11, 5 + len(str(index + 1)) * 4)
            outline = "#f59e0b" if self.selected == index else "white"
            self.canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline=outline, width=3)
            self.canvas.create_text(x, y, text=str(index + 1), fill="white")
        warning = " · 部分底圖無法載入" if visible & self.failed else ""
        message = f"{len(self.points)} 個路線點"
        if self.single:
            message = "已選定定點" if self.points else "尚未選定定點"
        else:
            state = "normal" if self.selected is not None else "disabled"
            self.insert_button.configure(state=state)
            self.delete_button.configure(state=state)
            if self.insert_at is not None:
                message = f"點地圖以插入第 {self.insert_at + 1} 點 · Esc 取消"
            elif self.selected is not None:
                message += f" · 選取第 {self.selected + 1} 點"
        self.status.set(message + warning)

    def poll_tiles(self):
        changed = False
        for key, future in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[key]
            try:
                self.images[key] = tk.PhotoImage(master=self, data=future.result())
            except (OSError, ValueError, tk.TclError):
                self.failed.add(key)
            changed = True
        if changed:
            self.draw(fetch=self.press is None)
        self.poll_id = self.after(75, self.poll_tiles)

    def change_zoom(self, change, event=None):
        zoom = max(2, min(19, self.zoom + change))
        if zoom == self.zoom:
            return
        if event:
            dx = event.x - self.canvas.winfo_width() / 2
            dy = event.y - self.canvas.winfo_height() / 2
            ratio = 1 / (TILE_SIZE << self.zoom) - 1 / (TILE_SIZE << zoom)
            self.center = (self.center[0] + dx * ratio, max(0, min(1, self.center[1] + dy * ratio)))
        self.zoom = zoom
        self.failed.clear()
        self.draw()

    def start_drag(self, event):
        distances = [math.hypot(x - event.x, y - event.y) for x, y in self.point_positions()]
        marker = min(range(len(distances)), key=distances.__getitem__) if distances else None
        if marker is not None and distances[marker] > 13:
            marker = None
        self.press = (event.x, event.y, self.center, marker)

    def drag(self, event):
        if self.press is None:
            return
        x, y, center, marker = self.press
        if marker is not None:
            self.canvas.delete("drag-preview")
            self.canvas.create_line(x, y, event.x, event.y, fill="#2563eb", dash=(4, 3), tags="drag-preview")
            return
        scale = TILE_SIZE << self.zoom
        self.center = (center[0] + (x - event.x) / scale, max(0, min(1, center[1] + (y - event.y) / scale)))
        self.draw(fetch=False)

    def finish_drag(self, event):
        if self.press is None:
            return
        x, y, center, marker = self.press
        self.press = None
        clicked = math.hypot(x - event.x, y - event.y) < 5
        if clicked:
            self.center = center
        if clicked and marker is not None and self.insert_at is None:
            self.selected = marker
        elif clicked or marker is not None:
            scale, left, top = self.origin()
            point = tuple(round(value, 7) for value in unproject((left + event.x) / scale, (top + event.y) / scale))
            points = self.points.copy()
            if self.single:
                points = [point]
                index = 0
            elif not clicked:
                points[marker] = point
                index = marker
            else:
                index = self.insert_at if self.insert_at is not None else len(points)
                points.insert(index, point)
            self.apply_edit(points, index)
        self.draw()

    def apply_edit(self, points, selected):
        if self.on_edit(points):
            self.points = points
            self.selected = selected
            self.insert_at = None

    def prepare_insert(self):
        self.insert_at = self.selected + 1
        self.draw()

    def cancel_insert(self, _event):
        self.insert_at = None
        self.draw()

    def delete_point(self):
        points = self.points.copy()
        del points[self.selected]
        self.apply_edit(points, None)
        self.draw()
