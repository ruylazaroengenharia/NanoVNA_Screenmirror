"""
Drivers dos aparelhos suportados (shell serial estilo ChibiOS, prompt "ch> ").

Cada driver descreve o aparelho (tela, pontos, faixa, slots de calibração, recursos)
e implementa a comunicação. A interface e as ferramentas usam só esta API:

    command(cmd, timeout=None) -> str
    capture() -> PIL.Image
    frequencies(), data(ch)
    acquire(start, stop, total_points, progress, cancel, restore) -> (f, s11, s21)
    restore_sweep(orig)
    meter_set(f), meter_read() -> list[complex]      (medidor S21 em frequência fixa)
    touch(x, y, action)                              (só aparelhos com tela de toque)
"""
import math
import threading
import time

import numpy as np
import serial
from PIL import Image

PROMPT = b"ch> "


class ShellVNA:
    NAME = "VNA"
    APP_NAME = "VNA Mirror"
    SCREEN = (320, 240)
    BIG_ENDIAN = True            # ordem dos bytes do RGB565 no 'capture'
    SEG_POINTS = 101             # pontos por varredura do firmware
    POINT_OPTIONS = (101,)
    F_MIN, F_MAX = 50e3, 900e6
    CAL_SLOTS = 5
    CAL_STEPS = (("Open", "cal open"), ("Short", "cal short"), ("Load", "cal load"),
                 ("Isolation", "cal isoln"), ("Thru", "cal thru"), ("Done", "cal done"))
    HAS_TOUCH = False
    HAS_TRACES = True
    HAS_GENERATOR = True
    HAS_NANOVNA_SYSTEM = True    # edelay, TDR (transform), power, saveconfig
    SNAPSHOT_SCALE = 3
    MAX_SEG = 10                 # limite de blocos de 101 pontos nas ferramentas
    INFO_MATCH = ()

    READ_TICK = 0.1              # timeout fixo da porta; os prazos são controlados aqui

    def __init__(self, port):
        self.port = port
        self.lock = threading.Lock()
        self._open()

    def _open(self):
        # O timeout é definido UMA vez, na abertura. Alterar ser.timeout depois faz o
        # pyserial reconfigurar a porta (SetCommTimeouts/SetCommState no Windows), e
        # alguns aparelhos USB CDC respondem com "dispositivo não está funcionando".
        self.ser = serial.Serial(self.port, 115200, timeout=self.READ_TICK, write_timeout=5)
        with self.lock:
            self._drain()
            self.ser.write(b"\r")
            self._read_until_prompt(3)

    def reopen(self):
        """Fecha e reabre a porta (após uma falha de USB)."""
        try:
            self.ser.close()
        except Exception:
            pass
        time.sleep(0.5)
        self._open()

    # ---------------- comunicação básica ----------------
    def _drain(self):
        """Descarta bytes pendentes sem usar reset_input_buffer."""
        t_end = time.time() + 0.5
        while time.time() < t_end:
            n = self.ser.in_waiting
            if not n:
                break
            self.ser.read(n)

    def _read_until(self, term, timeout):
        """Lê até 'term'; o que vier depois fica em self._pending."""
        buf = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            n = self.ser.in_waiting
            chunk = self.ser.read(n if n else 1)
            if not chunk:
                continue
            k = max(0, len(buf) - len(term) + 1)
            buf += chunk
            i = buf.find(term, k)
            if i >= 0:
                self._pending = bytes(buf[i + len(term):])
                return bytes(buf[:i + len(term)])
        raise TimeoutError("o aparelho não respondeu a tempo")

    def _read_exact(self, n, timeout):
        buf = bytearray(getattr(self, "_pending", b""))
        self._pending = b""
        deadline = time.time() + timeout
        while len(buf) < n and time.time() < deadline:
            chunk = self.ser.read(min(n - len(buf), max(1, self.ser.in_waiting)))
            if chunk:
                buf += chunk
        if len(buf) < n:
            raise TimeoutError(f"Quadro incompleto ({len(buf)}/{n} bytes)")
        self._pending = bytes(buf[n:])
        return bytes(buf[:n])

    def _read_until_prompt(self, timeout=2.0):
        self._pending = b""
        try:
            data = self._read_until(PROMPT, timeout)
        except TimeoutError:
            raise TimeoutError("o aparelho não respondeu (prompt 'ch>' não recebido)")
        return data[: -len(PROMPT)]

    def command(self, cmd, timeout=None):
        """Envia um comando de texto e devolve a resposta (sem eco e sem prompt)."""
        with self.lock:
            self._drain()
            self.ser.write(cmd.encode("ascii") + b"\r")
            data = self._read_until_prompt(timeout or 2.0).decode("ascii", errors="replace")
        lines = data.replace("\r", "").split("\n")
        if lines and lines[0].strip() == cmd.strip():
            lines = lines[1:]
        return "\n".join(lines).strip()

    @property
    def frame_bytes(self):
        return self.SCREEN[0] * self.SCREEN[1] * 2

    def capture(self):
        w, h = self.SCREEN
        with self.lock:
            self._drain()
            self.ser.write(b"capture\r")
            self._pending = b""
            echo = self._read_until(b"\n", 3)
            if b"capture" not in echo:
                raise IOError("Resposta inesperada ao comando capture")
            raw = self._read_exact(self.frame_bytes, max(3.0, self.frame_bytes / 100_000))
            tail = self._pending
            if PROMPT not in tail:
                self._read_until(PROMPT, 3)
            self._pending = b""
        px = np.frombuffer(raw, dtype=">u2" if self.BIG_ENDIAN else "<u2").reshape(h, w)
        rgb = np.empty((h, w, 3), dtype=np.uint8)
        rgb[..., 0] = ((px >> 11) & 0x1F) << 3
        rgb[..., 1] = ((px >> 5) & 0x3F) << 2
        rgb[..., 2] = (px & 0x1F) << 3
        return Image.fromarray(rgb, "RGB")

    def frequencies(self):
        return [int(x) for x in self.command("frequencies").split() if x.strip()]

    def data(self, ch):
        vals = []
        for line in self.command(f"data {ch}").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                vals.append(complex(float(parts[0]), float(parts[1])))
        return vals

    def info_text(self):
        try:
            return self.command("info")
        except Exception:
            return ""

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    # ---------------- varredura ----------------
    def set_sweep(self, start, stop, points=None):
        self.command(f"sweep {int(start)} {int(stop)}")

    def _read_segment(self, start, stop, n):
        f = self.frequencies()
        if len(f) < 2 or abs(f[0] - start) > 2 or abs(f[-1] - stop) > 2:
            raise IOError("faixa lida não confere")
        s11, s21 = self.data(0), self.data(1)
        m = min(len(f), len(s11), len(s21))
        return f[:m], s11[:m], s21[:m]

    def scan_segment(self, start, stop, n):
        raise NotImplementedError

    def chunks(self, start, stop, total):
        """Divide a grade uniforme de 'total' pontos em blocos que o firmware aceita."""
        df = (stop - start) / (total - 1)
        per = self.SEG_POINTS
        n_ch = int(math.ceil(total / per))
        sizes = [total // n_ch + (1 if k < total % n_ch else 0) for k in range(n_ch)]
        out, k0 = [], 0
        for sz in sizes:
            out.append((int(round(start + k0 * df)), int(round(start + (k0 + sz - 1) * df)), sz))
            k0 += sz
        return out

    def acquire(self, start, stop, total_points=101, progress=None, cancel=None, restore=True):
        orig = self.command("sweep").split()
        segs = self.chunks(start, stop, max(int(total_points), 2))
        F, A, B = [], [], []
        try:
            for k, (a, b, n) in enumerate(segs):
                if cancel is not None and cancel():
                    raise RuntimeError("cancelado")
                f, s11, s21 = self.scan_segment(a, b, n)
                F += f; A += s11; B += s21
                if progress:
                    progress(k + 1, len(segs))
        finally:
            if restore:
                self.restore_sweep(orig)
        return np.array(F, float), np.array(A, complex), np.array(B, complex)

    def restore_sweep(self, orig):
        if len(orig) >= 2 and orig[0].isdigit() and orig[1].isdigit():
            pts = orig[2] if len(orig) >= 3 and orig[2].isdigit() else None
            self.set_sweep(orig[0], orig[1], pts)
        self.command("resume")

    # ---------------- medidor S21 (frequência fixa) ----------------
    def meter_set(self, f):
        self.command(f"sweep {int(f)} {int(f)}")
        self.command("resume")

    def meter_read(self):
        return self.data(1)

    # ---------------- toque ----------------
    def touch(self, x, y, action):
        raise NotImplementedError


class NanoVNAH(ShellVNA):
    NAME = "NanoVNA-H"
    APP_NAME = "NanoVNA Mirror"
    SCREEN = (320, 240)
    BIG_ENDIAN = True
    SEG_POINTS = 101
    POINT_OPTIONS = (101,)
    F_MIN, F_MAX = 50e3, 900e6
    CAL_SLOTS = 5
    INFO_MATCH = ("NanoVNA",)

    def scan_segment(self, start, stop, n=101):
        """'scan' síncrono; se o firmware não aceitar, usa sweep + espera + pause."""
        if getattr(self, "_scan_ok", True):
            try:
                self.command(f"scan {int(start)} {int(stop)} 101", timeout=15)
                return self._read_segment(start, stop, n)
            except Exception:
                self._scan_ok = False
        self.command(f"sweep {int(start)} {int(stop)}")
        self.command("resume")
        time.sleep(2.5)
        self.command("pause")
        return self._read_segment(start, stop, n)


class SV4401A(ShellVNA):
    NAME = "SV4401A"
    APP_NAME = "SV4401A Mirror"
    SCREEN = (1024, 600)
    BIG_ENDIAN = False
    SEG_POINTS = 1001
    POINT_OPTIONS = (101, 201, 501, 1001)
    F_MIN, F_MAX = 50e3, 4.4e9
    CAL_SLOTS = 18
    CAL_STEPS = (("Open", "cal open"), ("Short", "cal short"), ("Load", "cal load"),
                 ("Thru", "cal thru"), ("Done", "cal done"))
    HAS_TOUCH = True
    HAS_TRACES = False
    HAS_GENERATOR = False
    HAS_NANOVNA_SYSTEM = False
    SNAPSHOT_SCALE = 1
    MAX_SEG = 20
    INFO_MATCH = ("SV4401A", "SV6301A")
    TOUCH_FORMATS = ("touch {x},{y} {ACT}", "touch {x} {y} {ACT}", "touch {x},{y}{ACT}")

    def __init__(self, port):
        super().__init__(port)
        self.touch_format = self.TOUCH_FORMATS[0]

    def set_sweep(self, start, stop, points=None):
        cmd = f"sweep {int(start)} {int(stop)}"
        if points:
            cmd += f" {int(points)}"
        self.command(cmd)

    def scan_segment(self, start, stop, n):
        """scan com outmask 7: cada linha = frequência, Re/Im S11, Re/Im S21."""
        n = max(101, min(1001, int(n)))
        txt = self.command(f"scan {int(start)} {int(stop)} {n} 7", timeout=60)
        f, a, b = [], [], []
        for line in txt.splitlines():
            p = line.split()
            if len(p) >= 5:
                try:
                    f.append(int(float(p[0])))
                    a.append(complex(float(p[1]), float(p[2])))
                    b.append(complex(float(p[3]), float(p[4])))
                except ValueError:
                    continue
        if len(f) < 2 or abs(f[0] - start) > 2 or abs(f[-1] - stop) > 2:
            # firmware sem outmask: lê pelos comandos separados
            return self._read_segment(start, stop, n)
        return f, a, b

    def chunks(self, start, stop, total):
        out = super().chunks(start, stop, total)
        # o firmware aceita no mínimo 101 pontos por scan
        if any(n < 101 for _, _, n in out):
            out = [(int(start), int(stop), max(101, min(1001, total)))]
        return out

    def meter_set(self, f):
        self._meter_f = int(f)
        self.command("pause")

    def meter_read(self):
        f = self._meter_f
        txt = self.command(f"scan {f} {f} 101 4", timeout=10)
        vals = []
        for line in txt.splitlines():
            p = line.split()
            if len(p) >= 2:
                try:
                    vals.append(complex(float(p[0]), float(p[1])))
                except ValueError:
                    pass
        return vals or self.data(1)

    def touch_usage(self):
        """Pergunta a sintaxe ao firmware (o comando sem argumentos imprime o uso)."""
        try:
            return self.command("touch", timeout=2)
        except Exception as e:
            return f"(sem resposta: {e})"

    def touch(self, x, y, action):
        """action: 'down', 'up' ou 'tap'."""
        x = max(0, min(self.SCREEN[0] - 1, int(x)))
        y = max(0, min(self.SCREEN[1] - 1, int(y)))
        acts = {"down": ["DOWN"], "up": ["UP"], "tap": ["DOWN", "UP"]}[action]
        for act in acts:
            self.command(self.touch_format.format(x=x, y=y, ACT=act))


DEVICES = {"NanoVNA-H": NanoVNAH, "SV4401A": SV4401A}
