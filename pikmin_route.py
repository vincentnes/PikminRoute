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


def parse_route(value):
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
        if not points or points[-1] != point:
            points.append(point)
    if len(points) < 2:
        raise ValueError("請輸入至少兩個不同的座標，每行一個。")
    return points


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


async def play_route(location, first_point, segments, stop, status):
    try:
        if stop.is_set():
            return False
        await asyncio.wait_for(location.set(*first_point), timeout=8)
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
                rsd = await stack.enter_async_context(UserspaceRsdTunnel(serial=serial, remotepairing_fallback=False))
                dvt = await stack.enter_async_context(DvtProvider(rsd))
                location = await stack.enter_async_context(LocationSimulation(dvt))
        except InvalidServiceError as error:
            raise RuntimeError(
                "iPhone 尚未提供所需的開發服務。確認開發者模式已開啟，並依 README 掛載開發者映像。"
            ) from error
        except TimeoutError as error:
            raise RuntimeError("連線逾時。請解鎖 iPhone、確認已信任這部電腦，再試一次。") from error
        if action == "check":
            return f"已連接 iPhone · iOS {rsd.product_version} · 定位服務可用"
        if action == "clear":
            await asyncio.wait_for(location.clear(), timeout=8)
            return "已送出恢復定位指令。請在 Apple 地圖確認。"
        finished = await play_route(location, points[0], segments, stop, status)
        result = "路線結束" if finished else "已停止"
        return f"{result}。已送出恢復定位指令，請在 Apple 地圖確認。"


class RouteWindow:
    def __init__(self, root):
        self.root = root
        self.messages = queue.SimpleQueue()
        self.stop = threading.Event()
        self.action = None
        self.closing = False
        self.status = tk.StringVar(value="請接上 iPhone，然後按「檢查連線」。")
        self.speed = tk.StringVar(value="15")

        root.title("iPhone 路線模擬器 v0.2")
        root.minsize(570, 335)
        root.protocol("WM_DELETE_WINDOW", self.close)
        body = ttk.Frame(root, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="iPhone 路線模擬器", font=("", 16, "bold")).pack(anchor="w")
        ttk.Label(body, text="路線座標（每行一個，格式為 緯度, 經度）").pack(anchor="w", pady=(12, 4))
        self.route = tk.Text(body, height=7, width=55, wrap="none")
        self.route.pack(fill="both", expand=True)

        settings = ttk.Frame(body)
        settings.pack(fill="x", pady=10)
        ttk.Label(settings, text="速度").pack(side="left")
        ttk.Entry(settings, textvariable=self.speed, width=8).pack(side="left", padx=6)
        ttk.Label(settings, text="km/h").pack(side="left")

        actions = ttk.Frame(body)
        actions.pack(fill="x")
        self.buttons = {}
        for key, label in (("check", "檢查連線"), ("play", "開始路線"), ("clear", "恢復定位")):
            button = ttk.Button(actions, text=label, command=lambda key=key: self.start(key))
            button.pack(side="left", padx=(0, 5))
            self.buttons[key] = button
        self.stop_button = ttk.Button(actions, text="停止並恢復", command=self.request_stop, state="disabled")
        self.stop_button.pack(side="left")

        files = ttk.Frame(body)
        files.pack(fill="x", pady=(8, 0))
        for key, label, handler in (
            ("save", "儲存路線", self.save_route),
            ("load", "載入路線", self.load_route),
        ):
            button = ttk.Button(files, text=label, command=handler)
            button.pack(side="left", padx=(0, 5))
            self.buttons[key] = button
        ttk.Label(body, textvariable=self.status, wraplength=525).pack(anchor="w", pady=(12, 0))
        root.after(100, self.poll)

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
            defaultextension=".json",
            filetypes=[("路線檔", "*.json")],
        )
        if not filename:
            return
        path = Path(filename)
        temporary = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
                temporary = Path(stream.name)
                json.dump({"coordinates": text, "speed_kmh": speed_text}, stream, ensure_ascii=False, indent=2)
            temporary.replace(path)
        except OSError as error:
            messagebox.showerror("儲存失敗", str(error))
            return
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)
        self.status.set(f"已儲存路線 · {path.name}")

    def load_route(self):
        filename = filedialog.askopenfilename(title="載入路線", filetypes=[("路線檔", "*.json")])
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
        self.action = action
        self.stop.clear()
        for button in self.buttons.values():
            button.configure(state="disabled")
        if action == "play":
            self.stop_button.configure(state="normal")
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
        self.stop_button.configure(state="disabled")
        self.status.set("正在停止，請保持 iPhone 連線…")

    def close(self):
        if self.action == "play":
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
            self.stop_button.configure(state="disabled")
            for button in self.buttons.values():
                button.configure(state="normal")
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
