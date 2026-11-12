import asyncio
from contextlib import AsyncExitStack, suppress
import json
import math
from pathlib import Path
import queue
import sys
import threading
from tempfile import NamedTemporaryFile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from route_map import RouteMap


def parse_route(value, *, minimum_points=2, deduplicate=True):
    points = []
    for line_number, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        try:
            lat, lon = map(float, line.replace("，", ",").split(","))
        except ValueError as error:
            raise ValueError(f"第 {line_number} 行請輸入「緯度, 經度」。") from error
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError(f"第 {line_number} 行的座標超出範圍。")
        point = (lat, lon)
        if not deduplicate or not points or points[-1] != point:
            points.append(point)
    if len(points) < minimum_points:
        raise ValueError("請輸入至少兩個不同的座標，每行一個。")
    return points


def parse_hold_point(value):
    points = parse_route(value, minimum_points=0, deduplicate=False)
    if len(points) != 1:
        raise ValueError("請輸入一組定點座標，格式為「緯度, 經度」。")
    return points[0]


def format_point(point):
    latitude, longitude = point
    return f"{latitude}, {longitude}"


def write_json(path, data):
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
        temporary.replace(path)
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def route_segments(points, speed_kmh):
    if not 0.1 <= speed_kmh <= 100:
        raise ValueError("速度請輸入 0.1 至 100 km/h。")
    for start, end in zip(points, points[1:]):
        lat1, lon1 = map(math.radians, start)
        lat2, lon2 = map(math.radians, end)
        delta_lon = lon2 - lon1
        hav = math.sin((lat2 - lat1) / 2) ** 2
        hav += math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
        arc = 2 * math.asin(math.sqrt(min(1.0, max(0.0, hav))))
        if arc < 1e-12:
            continue
        if math.pi - arc < 1e-8:
            raise ValueError("有兩個座標接近地球的對面，請在中間加入路線點。")
        bearing = math.atan2(
            math.sin(delta_lon) * math.cos(lat2),
            math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon),
        )
        seconds = arc * 6_371_008.8 / (speed_kmh / 3.6)
        yield end, lat1, lon1, bearing, arc, seconds


def interpolate(lat, lon, bearing, arc):
    sin_lat = math.sin(lat) * math.cos(arc) + math.cos(lat) * math.sin(arc) * math.cos(bearing)
    next_lat = math.asin(max(-1.0, min(1.0, sin_lat)))
    next_lon = lon + math.atan2(
        math.sin(bearing) * math.sin(arc) * math.cos(lat),
        math.cos(arc) - math.sin(lat) * math.sin(next_lat),
    )
    return math.degrees(next_lat), (math.degrees(next_lon) + 180) % 360 - 180


async def play_route(location, first_point, segments, stop, status, *, hold=False):
    try:
        if stop.is_set():
            return False
        await asyncio.wait_for(location.set(*first_point), timeout=8)
        if hold:
            status("正在定點停留 · 按「停止並恢復」可結束")
            ticks = 0
            while not stop.is_set():
                await asyncio.sleep(0.25)
                ticks += 1
                if ticks == 20 and not stop.is_set():
                    await asyncio.wait_for(location.set(*first_point), timeout=8)
                    ticks = 0
            return False
        for index, (end, lat, lon, bearing, arc, duration) in enumerate(segments, 1):
            status(f"移動中 · 第 {index}/{len(segments)} 段")
            elapsed = 0.0
            while elapsed < duration:
                if stop.is_set():
                    return False
                interval = min(1.0, duration - elapsed)
                await asyncio.sleep(interval)
                if stop.is_set():
                    return False
                elapsed += interval
                point = end if elapsed >= duration else interpolate(lat, lon, bearing, arc * elapsed / duration)
                await asyncio.wait_for(location.set(*point), timeout=8)
        return True
    finally:
        try:
            await asyncio.wait_for(location.clear(), timeout=8)
        except Exception as error:
            raise RuntimeError(
                "無法送出恢復定位指令。請重新接上同一支 iPhone，再按「恢復定位」。"
                "若仍無法恢復，請重新啟動 iPhone，再用 Apple 地圖確認。"
            ) from error


async def run_on_phone(action, points, segments, stop, status):
    from pymobiledevice3.exceptions import InvalidServiceError
    from pymobiledevice3.remote.userspace_tunnel import UserspaceRsdTunnel
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation
    from pymobiledevice3.usbmux import list_devices

    async with AsyncExitStack() as stack:
        try:
            async with asyncio.timeout(45):
                devices = {device.serial for device in await list_devices() if device.is_usb}
                if len(devices) != 1:
                    raise RuntimeError("請用 USB 資料線接上一支已解鎖的 iPhone。一次只接一支。")
                serial = next(iter(devices))
                if action == "reveal":
                    from pymobiledevice3.lockdown import create_using_usbmux
                    from pymobiledevice3.services.amfi import AmfiService

                    lockdown = await create_using_usbmux(serial=serial, autopair=False)
                    stack.push_async_callback(lockdown.close)
                    await AmfiService(lockdown).reveal_developer_mode_option_in_ui()
                    return "已送出顯示請求。請到 iPhone 的「設定 → 隱私權與安全性」開啟開發者模式。"
                rsd = await stack.enter_async_context(UserspaceRsdTunnel(serial=serial, remotepairing_fallback=False))
                dvt = await stack.enter_async_context(DvtProvider(rsd))
                location = await stack.enter_async_context(LocationSimulation(dvt))
        except InvalidServiceError as error:
            raise RuntimeError(
                "iPhone 尚未提供所需的開發服務。若手機上找不到開發者模式，請按「顯示開發者模式」。"
                "開啟後如仍無法連線，請依 README 掛載開發者映像。"
            ) from error
        except TimeoutError as error:
            raise RuntimeError("連線逾時。請解鎖 iPhone、確認已信任這部電腦，再試一次。") from error
        if action == "check":
            return f"已連接 iPhone · iOS {rsd.product_version} · 定位服務可用"
        if action == "clear":
            await asyncio.wait_for(location.clear(), timeout=8)
            return "已送出恢復定位指令。請在 Apple 地圖確認。"
        finished = await play_route(location, points[0], segments, stop, status, hold=action == "hold")
        result = "路線結束" if finished else "已停止"
        return f"{result}。已送出恢復定位指令，請在 Apple 地圖確認。"


class RouteWindow:
    def __init__(self, root):
        self.root = root
        self.messages = queue.SimpleQueue()
        self.stop = threading.Event()
        self.action = None
        self.route_map = None
        self.hold_map = None
        self.editor_window = None
        self.closing = False
        self.status = tk.StringVar(value="請接上 iPhone，然後按「檢查連線」。")
        self.speed = tk.StringVar(value="18")
        self.hold_coordinates = tk.StringVar()
        self.buttons = {}

        root.title("iPhone 路線模擬器 v0.5")
        root.geometry("450x470")
        root.minsize(450, 470)
        root.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(root, padding=8)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="iPhone 路線模擬器", font=("Noto Sans TC", 14, "bold")).pack(anchor="w")
        connection = ttk.Frame(body)
        connection.pack(fill="x", pady=(4, 6))
        self.add_button(connection, "check", "檢查連線", lambda: self.start("check"))
        self.add_button(connection, "reveal", "顯示開發者模式", lambda: self.start("reveal"))

        walking = tk.LabelFrame(
            body, text="走路路線", foreground="#355f8c", borderwidth=0,
            highlightbackground="#a9c9ed", highlightcolor="#a9c9ed", highlightthickness=2,
            relief="flat", padx=6, pady=5, font=("Noto Sans TC", 9, "bold"),
        )
        walking.pack(fill="both", expand=True)
        heading = ttk.Frame(walking)
        heading.pack(fill="x", pady=(0, 3))
        ttk.Label(heading, text="路線座標（緯度, 經度）").pack(side="left")
        self.add_button(heading, "detach", "獨立視窗", self.toggle_editor)
        self.editor_host = ttk.Frame(walking)
        self.editor_host.pack(fill="both", expand=True)
        self.editor_placeholder = ttk.Label(self.editor_host, text="座標已在獨立視窗顯示", anchor="center")
        self.create_route_editor(self.editor_host)

        files = ttk.Frame(walking)
        files.pack(fill="x", pady=(4, 0))
        self.add_button(files, "load", "載入路線", self.load_route)
        self.add_button(files, "save", "儲存路線", self.save_route)
        self.add_button(files, "map", "地圖預覽", self.open_map)

        settings = ttk.Frame(walking)
        settings.pack(fill="x", pady=(4, 0))
        ttk.Label(settings, text="速度").pack(side="left")
        ttk.Entry(settings, textvariable=self.speed, width=5).pack(side="left", padx=4)
        ttk.Label(settings, text="km/h").pack(side="left")

        play = ttk.Frame(settings)
        play.pack(side="left", padx=(8, 0))
        self.add_button(play, "play", "開始種花路線", lambda: self.start("play"))

        hold = tk.LabelFrame(
            body, text="定點停留", foreground="#355f8c", borderwidth=0,
            highlightbackground="#a9c9ed", highlightcolor="#a9c9ed", highlightthickness=2,
            relief="flat", padx=6, pady=5, font=("Noto Sans TC", 9, "bold"),
        )
        hold.pack(fill="x", pady=(6, 0))
        coordinates = ttk.Frame(hold)
        coordinates.pack(fill="x")
        ttk.Label(coordinates, text="定點座標").pack(side="left")
        ttk.Entry(coordinates, textvariable=self.hold_coordinates, width=1).pack(
            side="left", fill="x", expand=True, padx=4
        )
        self.add_button(coordinates, "hold_map", "地圖選點", self.open_hold_map)
        self.hold_coordinates.trace_add("write", self.refresh_hold_map)
        hold_actions = ttk.Frame(hold)
        hold_actions.pack(fill="x", pady=(4, 0))
        self.add_button(hold_actions, "load_point", "載入定點", self.load_point)
        self.add_button(hold_actions, "save_point", "儲存定點", self.save_point)
        self.add_button(hold_actions, "clear_point", "清除座標", self.clear_point)
        self.add_button(hold_actions, "hold", "開始停留", lambda: self.start("hold"))

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(6, 0))
        self.stop_button = self.make_button(footer, "停止並恢復", self.request_stop, "#fee2e2", "#991b1b", roomy=True, border=True)
        self.set_button_state(self.stop_button, "disabled")
        self.stop_button.pack(side="left", padx=(0, 5))
        self.add_button(footer, "clear", "恢復定位", lambda: self.start("clear"))
        ttk.Label(footer, text="斷線重接後補送", foreground="#64748b").pack(side="left", padx=2)
        ttk.Label(body, textvariable=self.status, wraplength=430).pack(anchor="w", pady=(4, 0))
        root.after(100, self.poll)

    def make_button(self, parent, label, handler, color, foreground, *, roomy=False, border=False):
        button = tk.Button(
            parent, text=label, command=handler, background=color, foreground=foreground,
            activebackground=color, activeforeground=foreground, disabledforeground="#7b8492",
            relief="solid" if border else "flat", borderwidth=1 if border else 0,
            padx=6, pady=4 if roomy else 2,
            font=("Noto Sans TC", 9), cursor="hand2",
        )
        button.normal_background = color
        return button

    def set_button_state(self, button, state):
        button.configure(state=state, background="#e5e7eb" if state == "disabled" else button.normal_background)

    def add_button(self, parent, key, label, handler):
        color, foreground = "#dbeafe", "#1e40af"
        if key in ("check", "reveal"):
            color, foreground = "#e2eaf3", "#334d68"
        elif key in ("play", "hold"):
            color, foreground = "#dcfce7", "#166534"
        elif key == "clear":
            color, foreground = "#ffedd5", "#9a3412"
        button = self.make_button(
            parent, label, handler, color, foreground,
            roomy=key in ("check", "reveal"), border=key == "clear",
        )
        button.pack(side="left", padx=(0, 3))
        self.buttons[key] = button

    def clear_route(self):
        self.route.edit_separator()
        self.route.delete("1.0", "end")
        self.route.edit_separator()
        self.refresh_map()
        self.draw_line_numbers()
        self.status.set("已清除畫面上的路線座標。")

    def clear_point(self):
        self.hold_coordinates.set("")
        self.status.set("已清除畫面上的定點座標。")

    def create_route_editor(self, parent):
        self.editor_panel = ttk.Frame(parent)
        self.editor_panel.pack(fill="both", expand=True)
        editor = ttk.Frame(self.editor_panel)
        editor.pack(side="left", fill="both", expand=True)
        ordering = ttk.Frame(self.editor_panel)
        ordering.pack(side="right", fill="y", padx=(4, 0))
        self.line_numbers = tk.Canvas(editor, width=28, height=1, background="#edf4fc", highlightthickness=0)
        self.line_numbers.pack(side="left", fill="y")
        scrollbar = ttk.Scrollbar(editor, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        horizontal = ttk.Scrollbar(editor, orient="horizontal")
        horizontal.pack(side="bottom", fill="x")
        self.route = tk.Text(
            editor, height=5, width=1, wrap="none", undo=True, exportselection=False,
            xscrollcommand=horizontal.set,
            yscrollcommand=lambda first, last: self.route_scrolled(scrollbar, first, last),
        )
        self.route.pack(side="left", fill="both", expand=True)
        scrollbar.configure(command=self.route.yview)
        horizontal.configure(command=self.route.xview)
        self.route.tag_configure("route-selected", background="#dbeafe")
        self.route.bind("<<Modified>>", self.route_text_changed)
        self.route.bind("<Configure>", lambda _: self.draw_line_numbers())
        self.route.bind("<ButtonRelease-1>", self.route_cursor_changed)
        self.route.bind("<KeyRelease>", self.route_cursor_changed)
        self.add_button(ordering, "up", "上移", lambda: self.move_route_point(-1))
        self.add_button(ordering, "down", "下移", lambda: self.move_route_point(1))
        self.add_button(ordering, "clear_route", "清除座標", self.clear_route)
        for button in ordering.winfo_children():
            button.pack_configure(side="top", fill="x", padx=0, pady=(0, 3))

    def transfer_route_editor(self, source):
        text = source.get("1.0", "end-1c")
        if self.route.get("1.0", "end-1c") != text:
            self.route.edit_separator()
            self.route.replace("1.0", "end-1c", text)
            self.route.edit_separator()
        self.route.mark_set("insert", source.index("insert"))
        self.route.xview_moveto(source.xview()[0])
        self.route.yview_moveto(source.yview()[0])
        self.route.edit_modified(False)
        self.draw_line_numbers()
        self.route_cursor_changed()

    def toggle_editor(self):
        if self.editor_window is not None and self.editor_window.winfo_exists():
            self.attach_editor()
            return
        self.editor_window = tk.Toplevel(self.root)
        self.editor_window.title("路線座標 · 獨立視窗")
        self.editor_window.geometry("760x560")
        self.editor_window.minsize(520, 300)
        self.editor_window.protocol("WM_DELETE_WINDOW", self.attach_editor)
        ttk.Label(
            self.editor_window, text="可直接貼上座標；關閉此視窗即可收回主畫面。", padding=8
        ).pack(fill="x")
        source = self.route
        self.docked_editor = (
            self.editor_panel, self.route, self.line_numbers,
            {key: self.buttons[key] for key in ("up", "down", "clear_route")},
        )
        self.editor_panel.pack_forget()
        self.editor_placeholder.pack(fill="both", expand=True)
        self.create_route_editor(self.editor_window)
        self.editor_panel.pack_configure(padx=8, pady=(0, 8))
        self.transfer_route_editor(source)
        self.route.edit_reset()
        self.buttons["detach"].configure(text="收回視窗")
        self.editor_window.lift()
        self.route.focus_set()
        self.draw_line_numbers()

    def attach_editor(self):
        source = self.route
        self.editor_panel, self.route, self.line_numbers, buttons = self.docked_editor
        self.buttons.update(buttons)
        for button in buttons.values():
            self.set_button_state(button, "disabled" if self.action else "normal")
        self.editor_placeholder.pack_forget()
        self.editor_panel.pack(fill="both", expand=True)
        self.transfer_route_editor(source)
        self.buttons["detach"].configure(text="獨立視窗")
        self.editor_window.destroy()
        self.editor_window = None
        self.draw_line_numbers()

    def open_map(self):
        try:
            points = self.map_points()
        except ValueError as error:
            messagebox.showerror("請檢查路線", str(error), parent=self.root)
            return
        if self.route_map is None or not self.route_map.winfo_exists():
            self.route_map = RouteMap(self.root, points, self.apply_map_route, on_select=self.select_route_point)
        else:
            self.route_map.set_points(points, fit=True)
        self.route_map.lift()
        self.route_cursor_changed()

    def map_points(self):
        return parse_route(self.route.get("1.0", "end"), minimum_points=0, deduplicate=False)

    def route_text_changed(self, _event):
        if _event.widget is not self.route:
            return
        if self.route.edit_modified():
            self.route.edit_modified(False)
            self.refresh_map()
            self.draw_line_numbers()
            self.route_cursor_changed()

    def route_scrolled(self, scrollbar, first, last):
        scrollbar.set(first, last)
        self.draw_line_numbers()

    def route_line_indices(self):
        lines = self.route.get("1.0", "end-1c").split("\n")
        return [index for index, line in enumerate(lines, 1) if line.strip()]

    def draw_line_numbers(self):
        self.line_numbers.delete("all")
        for number, line in enumerate(self.route_line_indices(), 1):
            position = self.route.dlineinfo(f"{line}.0")
            if position is not None:
                self.line_numbers.create_text(
                    24, position[1], anchor="ne", text=str(number), fill="#355f8c",
                    font=self.route.cget("font"),
                )

    def route_cursor_changed(self, _event=None):
        line = int(self.route.index("insert").split(".")[0])
        lines = self.route_line_indices()
        self.route.tag_remove("route-selected", "1.0", "end")
        if line not in lines:
            return
        self.route.tag_add("route-selected", f"{line}.0", f"{line}.end+1c")
        if self.route_map is not None and self.route_map.winfo_exists():
            self.route_map.select_point(lines.index(line), reveal=True)

    def select_route_point(self, index):
        lines = self.route_line_indices()
        if index is None or not 0 <= index < len(lines):
            return
        line = lines[index]
        self.route.mark_set("insert", f"{line}.0")
        self.route.see(f"{line}.0")
        self.route.tag_remove("route-selected", "1.0", "end")
        self.route.tag_add("route-selected", f"{line}.0", f"{line}.end+1c")
        self.draw_line_numbers()

    def move_route_point(self, direction):
        lines = self.route_line_indices()
        current = int(self.route.index("insert").split(".")[0])
        if current not in lines:
            return
        index = lines.index(current)
        target = index + direction
        if not 0 <= target < len(lines):
            return
        text = self.route.get("1.0", "end-1c").split("\n")
        destination = lines[target]
        text[current - 1], text[destination - 1] = text[destination - 1], text[current - 1]
        self.route.edit_separator()
        self.route.replace("1.0", "end-1c", "\n".join(text))
        self.route.edit_separator()
        self.route.mark_set("insert", f"{destination}.0")
        self.route.see("insert")
        self.refresh_map()
        self.draw_line_numbers()
        self.route_cursor_changed()

    def refresh_map(self, *, fit=False):
        if self.route_map is None or not self.route_map.winfo_exists():
            return
        try:
            points = self.map_points()
        except ValueError:
            points = []
        self.route_map.set_points(points, fit=fit)

    def apply_map_route(self, points):
        try:
            self.map_points()
        except ValueError as error:
            messagebox.showerror("請先修正座標欄", str(error), parent=self.route_map)
            return False
        self.route.replace("1.0", "end-1c", "\n".join(map(format_point, points)))
        return True

    def current_hold_points(self):
        try:
            return [parse_hold_point(self.hold_coordinates.get())]
        except ValueError:
            return []

    def open_hold_map(self):
        points = self.current_hold_points()
        if self.hold_map is None or not self.hold_map.winfo_exists():
            self.hold_map = RouteMap(self.root, points, self.apply_map_hold, single=True)
        else:
            self.hold_map.set_points(points, fit=True)
        self.hold_map.lift()

    def refresh_hold_map(self, *_args, fit=False):
        if self.hold_map is not None and self.hold_map.winfo_exists():
            self.hold_map.set_points(self.current_hold_points(), fit=fit)

    def apply_map_hold(self, points):
        self.hold_coordinates.set(format_point(points[0]))
        return True

    def validate_route(self, text, speed_text):
        points = parse_route(text)
        try:
            speed = float(speed_text)
        except (TypeError, ValueError) as error:
            raise ValueError("速度請輸入數字。") from error
        segments = list(route_segments(points, speed))
        if not segments:
            raise ValueError("這條路線沒有可移動的距離。")
        return points, segments

    def route_dialog_directory(self, folder="routes"):
        app_file = sys.executable if getattr(sys, "frozen", False) else __file__
        app_dir = Path(app_file).resolve().parent
        routes_dir = app_dir / folder
        try:
            routes_dir.mkdir(exist_ok=True)
        except OSError:
            return str(app_dir)
        return str(routes_dir)

    def save_route(self):
        text = self.route.get("1.0", "end").strip()
        speed_text = self.speed.get().strip()
        try:
            self.validate_route(text, speed_text)
        except ValueError as error:
            messagebox.showerror("請檢查路線或速度", str(error))
            return
        filename = filedialog.asksaveasfilename(
            title="儲存路線",
            initialdir=self.route_dialog_directory(),
            defaultextension=".json",
            filetypes=[("路線檔", "*.json")],
        )
        if not filename:
            return
        path = Path(filename)
        try:
            write_json(path, {"coordinates": text, "speed_kmh": speed_text})
        except OSError as error:
            messagebox.showerror("儲存失敗", str(error))
            return
        self.status.set(f"已儲存路線 · {path.name}")

    def load_route(self):
        filename = filedialog.askopenfilename(
            title="載入路線",
            initialdir=self.route_dialog_directory(),
            filetypes=[("路線檔", "*.json")],
        )
        if not filename:
            return
        path = Path(filename)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or not isinstance(data.get("coordinates"), str):
                raise ValueError("找不到有效的座標資料。")
            text = data["coordinates"]
            speed_text = str(data.get("speed_kmh", ""))
            self.validate_route(text, speed_text)
        except (OSError, ValueError) as error:
            messagebox.showerror("載入失敗", str(error))
            return
        if self.route.get("1.0", "end").strip() and not messagebox.askyesno(
            "載入路線", "要用檔案裡的座標與速度取代畫面上的內容嗎？"
        ):
            return
        self.route.delete("1.0", "end")
        self.route.insert("1.0", text)
        self.speed.set(speed_text)
        self.status.set(f"已載入路線 · {path.name}")
        self.refresh_map(fit=True)

    def save_point(self):
        try:
            point = parse_hold_point(self.hold_coordinates.get())
        except ValueError as error:
            messagebox.showerror("請檢查定點", str(error), parent=self.root)
            return
        filename = filedialog.asksaveasfilename(
            parent=self.root,
            title="命名並儲存定點",
            initialdir=self.route_dialog_directory("points"),
            defaultextension=".json",
            filetypes=[("定點檔", "*.json")],
        )
        if not filename:
            return
        path = Path(filename)
        try:
            write_json(path, {"type": "hold_point", "coordinates": format_point(point)})
        except OSError as error:
            messagebox.showerror("儲存定點失敗", str(error), parent=self.root)
            return
        self.status.set(f"已儲存定點 · {path.stem}")

    def load_point(self):
        filename = filedialog.askopenfilename(
            parent=self.root,
            title="載入定點",
            initialdir=self.route_dialog_directory("points"),
            filetypes=[("定點檔", "*.json")],
        )
        if not filename:
            return
        path = Path(filename)
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict) or data.get("type") != "hold_point":
                raise ValueError("請選擇用「儲存定點」建立的檔案。")
            coordinates = data.get("coordinates")
            if not isinstance(coordinates, str):
                raise ValueError("找不到有效的定點座標。")
            point = parse_hold_point(coordinates)
        except (OSError, ValueError) as error:
            messagebox.showerror("載入定點失敗", str(error), parent=self.root)
            return
        self.hold_coordinates.set(format_point(point))
        self.refresh_hold_map(fit=True)
        self.status.set(f"已載入定點 · {path.stem}")

    def start(self, action):
        if self.action:
            return
        points, segments = [], []
        if action == "play":
            try:
                points, segments = self.validate_route(self.route.get("1.0", "end"), self.speed.get())
            except ValueError as error:
                messagebox.showerror("請檢查路線或速度", str(error))
                return
        elif action == "hold":
            try:
                points = [parse_hold_point(self.hold_coordinates.get())]
            except ValueError as error:
                messagebox.showerror("請檢查定點", str(error), parent=self.root)
                return
        self.action = action
        self.stop.clear()
        for button in self.buttons.values():
            self.set_button_state(button, "disabled")
        if action in ("play", "hold"):
            self.set_button_state(self.stop_button, "normal")
        self.status.set("正在連接 iPhone…")
        threading.Thread(target=self.work, args=(action, points, segments), daemon=True).start()

    def work(self, action, points, segments):
        def send_status(text):
            self.messages.put(("status", text))

        try:
            result = asyncio.run(run_on_phone(action, points, segments, self.stop, send_status))
        except Exception as error:
            self.messages.put(("error", f"{type(error).__name__}: {error}"))
        else:
            self.messages.put(("done", result))

    def request_stop(self):
        self.stop.set()
        self.set_button_state(self.stop_button, "disabled")
        self.status.set("正在停止，請保持 iPhone 連線…")

    def close(self):
        if self.action in ("play", "hold"):
            self.closing = True
            self.request_stop()
        elif self.action:
            self.closing = True
        else:
            self.root.destroy()

    def poll(self):
        while not self.messages.empty():
            kind, text = self.messages.get_nowait()
            if kind == "status":
                if not self.stop.is_set():
                    self.status.set(text)
                continue
            self.action = None
            self.set_button_state(self.stop_button, "disabled")
            for button in self.buttons.values():
                self.set_button_state(button, "normal")
            if kind == "error":
                self.closing = False
                self.status.set("操作未完成。請依錯誤訊息檢查。")
                messagebox.showerror("操作未完成", text)
            else:
                self.status.set(text)
                if self.closing:
                    self.root.destroy()
                    return
        self.root.after(100, self.poll)


if __name__ == "__main__":
    if sys.argv[1:2] == ["mounter"]:
        from pymobiledevice3.__main__ import main

        sys.exit(main())

    root = tk.Tk()
    RouteWindow(root)
    root.mainloop()
