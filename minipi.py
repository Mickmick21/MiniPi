#!/usr/bin/env python3
# MiniPi - Minitel interface for Raspberry Pi
# GPIO UART (ttyAMA0) @ 1200 baud, 7E1
# (C) 2026 Mickmick.bin - GNU General Public License v3.0
# License available at https://choosealicense.com/licenses/gpl-3.0/

import serial
import time
import subprocess
import socket
import os
import urllib.request
import json
import tempfile
import websocket
import threading
from urllib.parse import unquote
import re
import pty
import select
import sys

ANSI_RE = re.compile(r'\x1b\[([0-9;]*)m')

WS = None
WS_RUNNING = False
WS_URL = None
WS_STATE = "DISCONNECTED"   # DISCONNECTED | CONNECTING | CONNECTED | CLOSED | ERROR
WS_LAST_CLOSE_INFO = ""

APP_NAME = "MiniPi"
APP_VERSION = "1.1.0"
APP_UA = f"{APP_NAME}/{APP_VERSION}"

# Initialiser le serial.

ser = serial.Serial(
    "/dev/ttyAMA0",
    baudrate=1200,
    bytesize=7,
    parity='E',
    stopbits=1,
    timeout=0.1
)

# Couleurs.

NOIR    = 0
ROUGE   = 1
VERT    = 2
JAUNE   = 3
BLEU    = 4
MAGENTA = 5
CYAN    = 6
BLANC   = 7

def ansi_apply(text: str, draw_char):
    """
    ansi_to_minitel mais en streaming.
    """

    fg = BLANC
    bg = NOIR

    i = 0
    while i < len(text):

        if text[i] == '\x1b' and i + 1 < len(text) and text[i+1] == '[':
            j = i + 2
            seq = ''

            while j < len(text) and text[j] != 'm':
                seq += text[j]
                j += 1

            if j < len(text):
                codes = seq.split(';')

                for c in codes:
                    if not c:
                        continue
                    n = int(c)

                    if n == 0:
                        fg = BLANC
                        bg = NOIR

                    elif 30 <= n <= 37:
                        fg = n - 30

                    elif 40 <= n <= 47:
                        bg = n - 40

            i = j + 1
            continue

        draw_char(text[i], fg, bg)
        i += 1

def ansi_to_minitel(text: str):
    """
    Convertion basique de codes couleurs ANSI vers des couleurs Minitel.
    Support:
      30–37 (fg)
      40–47 (bg)
      0 reset
    """
    parts = ANSI_RE.split(text)

    out = []
    i = 0

    while i < len(parts):
        chunk = parts[i]
        out.append(chunk)
        i += 1

        if i >= len(parts):
            break

        codes = parts[i]
        i += 1

        for code in codes.split(';'):
            if not code:
                continue

            c = int(code)

            # RESET
            if c == 0:
                color(BLANC)
                bgcolor(NOIR)

            # FG
            elif 30 <= c <= 37:
                color(c - 30)

            # BG
            elif 40 <= c <= 47:
                bgcolor(c - 40)

    return ''.join(out)

# Codes de touches fonctions. (SEP + chr(64+code))

KEY_ENVOI       = 1
KEY_RETOUR      = 2
KEY_REPETITION  = 3
KEY_GUIDE       = 4
KEY_ANNULATION  = 5
KEY_SOMMAIRE    = 6
KEY_CORRECTION  = 7
KEY_SUITE       = 8
KEY_CNXFIN      = 25

# ─────────────────────────────────────────────
#  SYSTEM HELPERS
# ─────────────────────────────────────────────

CONFIG_FILE = "/etc/minipi.conf"


def load_config():
    cfg = {}
    try:
        with open(CONFIG_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")

def sys_run(cmd):
    try:
        return subprocess.check_output(
            cmd,
            shell=True,
            stderr=subprocess.STDOUT
        ).decode(errors="ignore")
    except Exception as e:
        return str(e)
    
def sys_run_interactive(cmd, output_callback, input_callback=None):
    """
    Executer une commande dans un pseudo-terminal et streamer la sortie.
    output_callback(data) est appelé pour chaque chunks.
    input_callback() doit retourner les octets à envoyer, ou None.
    """
    pid, fd = pty.fork()

    if pid == 0:
        os.execvp("sh", ["sh", "-c", cmd])
    else:
        while True:
            r, _, _ = select.select([fd], [], [], 0.05)

            # Lire la sortie.
            if fd in r:
                try:
                    data = os.read(fd, 1024)
                    if not data:
                        break
                    output_callback(data.decode(errors="ignore"))
                except OSError:
                    break

            # Entrée.
            if input_callback:
                data = input_callback()
                if data:
                    try:
                        os.write(fd, data)
                    except OSError:
                        break

            # Processus terminé.
            try:
                pid_done, _ = os.waitpid(pid, os.WNOHANG)
                if pid_done != 0:
                    break
            except ChildProcessError:
                break

# Sortie Low-level

def _write_bytes(data: bytes):
    ser.write(data)

def send(text: str):
    """Envoyer du texte, avec convertition des accents."""
    text = _accents(text)
    _write_bytes(text.encode('latin-1', errors='ignore'))

def sendchr(code: int):
    _write_bytes(bytes([code]))

def sendesc(seq: str):
    sendchr(27)
    send(seq)

# Contrôle de l'écran.

def clear():
    """Effacer l'écran et bouger le curseur à sa maison."""
    sendchr(12)   # FF

def pos(ligne: int, colonne: int = 1):
    """Positioner le curseur (ligne, colonne), 1-indexed."""
    if ligne == 1 and colonne == 1:
        sendchr(30)   # RS = home
    else:
        sendchr(31)   # US
        sendchr(64 + ligne)
        sendchr(64 + colonne)

def cursor(visible: bool):
    sendchr(17 if visible else 20)

def color(c: int):
    """Changer la couleur d'avant-plan."""
    sendesc(chr(64 + c))

def bgcolor(c: int):
    """Changer la couleur d'arrière-plan"""
    sendesc(chr(80 + c))

def inverse(on: bool = True):
    sendesc('\x5D' if on else '\x5C')

def blink(on: bool = True):
    sendesc('\x48' if on else '\x49')

def underline(on: bool = True):
    sendesc(chr(90) if on else chr(89))

def bip():
    sendchr(7)

def eol(ligne: int, colonne: int = 1):
    """Effacer de (ligne, colonne) à la fin de la ligne."""
    pos(ligne, colonne)
    sendchr(24)   # CAN

def fill(char: str, count: int):
    """Répéter un charactère count foix avec REP quand possible."""
    if count <= 0:
        return
    send(char)
    if count == 1:
        return
    if count == 2:
        send(char)
        return
    remaining = count - 1
    while remaining > 0:
        n = min(remaining, 63)
        sendchr(18)          # REP
        sendchr(64 + n)
        remaining -= n

def hline(ligne: int, colonne: int, width: int, char: str = ' '):
    pos(ligne, colonne)
    fill(char, width)

# Conversion des accents, STUM 2.3.1 p.22 https://www.minitel-alcatel.fr/documents/M1_1983-1984/STUM%20M1.pdf 

def _accents(text: str) -> str:
    replacements = [
        ('à', '\x19\x41a'), ('â', '\x19\x43a'), ('ä', '\x19\x48a'),
        ('è', '\x19\x41e'), ('é', '\x19\x42e'), ('ê', '\x19\x43e'), ('ë', '\x19\x48e'),
        ('î', '\x19\x43i'), ('ï', '\x19\x48i'),
        ('ô', '\x19\x43o'), ('ö', '\x19\x48o'),
        ('ù', '\x19\x41u'), ('û', '\x19\x43u'), ('ü', '\x19\x48u'),
        ('ç', '\x19\x4Bc'),
        ('À', '\x19\x41A'),
        ('È', '\x19\x41E'),
        ('É', '\x19\x42E'),
        ('Î', '\x19\x43I'),
        ('Ô', '\x19\x43O'),
        ('Ù', '\x19\x41U'),
        ('Ç', '\x19\x4BC'),
        ('£', '\x19\x23'),
        ('°', '\x19\x30'),
        ('¼', '\x19\x3C'),
        ('½', '\x19\x3D'),
        ('¾', '\x19\x3E'),
        ('←', '\x19\x2C'),
        ('↑', '\x19\x2D'),
        ('→', '\x19\x2E'),
        ('↓', '\x19\x2F'),
        ('Œ', '\x19\x6A'),
        ('œ', '\x19\x7A'),
    ]

    for src, dst in replacements:
        text = text.replace(src, dst)

    return text

# Entrée

# Protocoles
_PRO1 = '\x1b\x39'
_PRO2 = '\x1b\x3a'
_PRO3 = '\x1b\x3b'


def read_event():
    """
    Lire un événement du Minitel.
    Retourne:
      ("KEY",  int)
      ("CHAR", str)
      ("ESC",  None)
      None
    """
    def debug(ev, raw):
        try:
            print(f"[RAW] {raw.hex()} ({list(raw)})")
            print(f"[EV ] {ev}")
        except:
            pass

    c = ser.read(1)
    if not c:
        return None

    raw = bytearray(c)
    b = c[0]

    if b == 0x1B:  # ESC
        nxt = ser.read(1)
        if nxt:
            raw += nxt
            nb = nxt[0]

            if nb == 0x39:  # PRO1
                extra = ser.read(1)
                raw += extra

            elif nb == 0x3A:  # PRO2
                extra = ser.read(2)
                raw += extra

            elif nb == 0x3B:  # PRO3
                extra = ser.read(3)
                raw += extra

        ev = ("ESC", None)
        debug(ev, raw)
        return ev

    if b == 0x13:  # SEP
        k = ser.read(1)
        if not k:
            return None

        raw += k
        code = k[0] - 64

        ev = ("KEY", code)
        debug(ev, raw)
        return ev

    try:
        ch = c.decode('latin-1')
        if ch >= ' ':
            ev = ("CHAR", ch)
            debug(ev, raw)
            return ev
    except Exception:
        pass

    debug(None, raw)
    return None


def read_input(ligne: int, colonne: int, longueur: int,
               data: str = '', char_fill: str = '.') -> tuple:
    """
    Blocage de la saisie de texte dans un seul champ.
    Affiche le champ, gère la modification, renvoie (text, key_code).
    key_code est la touche de fonction qui a terminé la saisie.
    """
    cursor(False)
    pos(ligne, colonne)
    send(data)
    fill(char_fill, longueur - len(data))
    pos(ligne, colonne + len(data))
    cursor(True)

    while True:
        ev = read_event()
        if ev is None:
            continue
        et, val = ev

        if et == "KEY":
            if val == KEY_CORRECTION and data:
                data = data[:-1]
                pos(ligne, colonne + len(data))
                send(char_fill)
                pos(ligne, colonne + len(data))
            elif val == KEY_ANNULATION:
                data = ''
                pos(ligne, colonne)
                fill(char_fill, longueur)
                pos(ligne, colonne)
            else:
                cursor(False)
                return (data, val)

        elif et == "CHAR":
            if len(data) < longueur:
                data += val
                send(val)
                if len(data) == longueur:
                    pos(ligne, colonne)
                    fill(char_fill, longueur)
                    pos(ligne, colonne)
                    send(data)
            else:
                bip()

#Interface utilisateur

WIDTH = 40

def textbg(ligne: int, colonne: int, text: str, bg: int, fg: int = BLANC):
    """
    Écrire du texte avec une couleur de fond sur une seule ligne.
    En Videotex, chaque cellule de caractère possède ses propres attributs; par conséquent, `bgcolor+color`
    doit être émis immédiatement avant CHAQUE caractère, et jamais juste avant un espace.
    Pour ce faire, nous envoyons les octets d'attribut avant la chaîne entière;
    ils restent actifs pour chaque caractère suivant jusqu'à ce qu'ils soient modifiés.
    L'appelant est responsable du remplissage de `text` pour atteindre la largeur souhaitée.
    """
    pos(ligne, colonne)
    sendesc(chr(80 + bg))
    sendesc(chr(64 + fg))
    send(' ')
    send(text.ljust(WIDTH - 1))
    sendesc(chr(64 + BLANC))
    sendesc(chr(80 + NOIR))

def header(title: str, bg: int = BLEU, fg: int = BLANC):
    """Tracer une barre de titre colorée sur toute la largeur de la ligne 1."""
    padded = title.center(WIDTH)
    textbg(1, 1, padded[:WIDTH], bg, fg)

def footer(text: str, bg: int = BLEU, fg: int = BLANC):
    """Tracer une barre d'indice colorée à la ligne 24."""
    padded = text.center(WIDTH - 1)
    textbg(24, 1, padded[:WIDTH - 1], bg, fg)

def status(message: str, ligne: int = 23, bg: int = JAUNE,
           fg: int = NOIR, delay: float = 1.5):
    """Afficher temporairement un message d'état coloré, puis l'effacer."""
    cursor(False)
    padded = message.center(WIDTH)
    textbg(ligne, 1, padded[:WIDTH], bg, fg)
    ser.flush()
    time.sleep(delay)
    textbg(ligne, 1, ' ' * WIDTH, NOIR, BLANC)

def box(top: int, left: int, height: int, width: int,
        bg: int = NOIR, fg: int = BLANC):
    """Remplire une zone rectangulaire avec une couleur de fond."""
    for l in range(top, top + height):
        textbg(l, left, ' ' * width, bg, fg)

# Infos système minifiés (utilisé par Accueil et Configuration)

def get_hostname() -> str:
    try:
        return socket.gethostname()
    except Exception:
        return '?'

def get_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return 'Pas d\'internet'

def get_uptime() -> str:
    try:
        with open('/proc/uptime') as f:
            secs = float(f.read().split()[0])
        h, m = divmod(int(secs) // 60, 60)
        return f'{h}h{m:02d}m'
    except Exception:
        return '?'

# Terminal

def app_shell():
    clear()
    header('Terminal', bg=VERT, fg=NOIR)
    textbg(2, 1, 'SOMMAIRE: quitter  SUITE/RETOUR: historique'.ljust(WIDTH), VERT, NOIR)

    cmd = ''
    history: list = []
    hist_idx = -1
    output_line = 4
    cursor_x = 0
    cursor_y = 4

    def prompt_line():
        nonlocal output_line
        if output_line > 22:
            output_line = 4
            clear()
            header('Terminal', bg=VERT, fg=NOIR)
        color(VERT)
        pos(output_line, 1)
        send('$ ')
        color(BLANC)

    def redraw_cmd():
        color(VERT)
        pos(output_line, 1)
        send('$ ')
        color(BLANC)
        send(cmd[:WIDTH - 2].ljust(WIDTH - 2))
        pos(output_line, 3 + len(cmd))

    def term_write(data):
        nonlocal cursor_x, cursor_y

        def draw(ch, fg, bg):
            nonlocal cursor_x, cursor_y

            color(fg)
            bgcolor(bg)

            if ch == '\n':
                cursor_x = 0
                cursor_y += 1
                return

            if ch == '\r':
                cursor_x = 0
                return

            if ch == '\x08':
                if cursor_x > 0:
                    cursor_x -= 1
                    pos(cursor_y, cursor_x + 1)
                    send(' ')
                    pos(cursor_y, cursor_x + 1)
                return

            if cursor_x >= WIDTH:
                cursor_x = 0
                cursor_y += 1

            if cursor_y > 22:
                for l in range(4, 23):
                    textbg(l, 1, ' ' * WIDTH, NOIR, BLANC)
                cursor_y = 4

            pos(cursor_y, cursor_x + 1)
            send(ch)
            cursor_x += 1

        ansi_apply(data, draw)


    def input_cb():
        ev = read_event()
        if not ev:
            return None

        et, val = ev

        if et == "CHAR":
            return val.encode()

        if et == "KEY":
            if val == KEY_ENVOI:
                return b"\n"
            elif val == KEY_CORRECTION:
                return b"\x7f"
            elif val == KEY_ANNULATION:
                return b"\x03"
            elif val == KEY_SOMMAIRE:
                return b"\x04"

        return None

    prompt_line()
    cursor(True)

    while True:
        ev = read_event()
        if ev is None:
            continue
        et, val = ev

        if et == "KEY":

            if val == KEY_ENVOI:
                send('\r\n')
                output_line += 1
                if cmd.strip():
                    history.append(cmd)
                    hist_idx = len(history)

                if cmd.strip() in ('exit', 'quit'):
                    break

                cursor(False)

                def print_stream(text):
                    nonlocal output_line

                    text = ansi_to_minitel(text)

                    for line in text.splitlines():
                        if output_line > 22:
                            output_line = 4
                            clear()
                            header('Terminal', bg=VERT, fg=NOIR)

                        pos(output_line, 1)
                        send(line[:WIDTH])
                        output_line += 1

                try:
                    cursor_y = output_line
                    cursor_x = 0
                    sys_run_interactive(
                        cmd,
                        lambda data: term_write(data),
                        input_cb
                    )
                    output_line = cursor_y + 1
                except Exception as e:
                    print_stream(f"[Erreur: {e}]")

                cmd = ''
                prompt_line()
                cursor(True)

            elif val == KEY_CORRECTION:
                if cmd:
                    cmd = cmd[:-1]
                    redraw_cmd()

            elif val == KEY_ANNULATION:
                cmd = ''
                redraw_cmd()

            elif val == KEY_SUITE:          # next history
                if history and hist_idx < len(history) - 1:
                    hist_idx += 1
                    cmd = history[hist_idx]
                    redraw_cmd()

            elif val == KEY_RETOUR:         # previous history
                if history and hist_idx > 0:
                    hist_idx -= 1
                    cmd = history[hist_idx]
                    redraw_cmd()

            elif val == KEY_SOMMAIRE:
                break

        elif et == "CHAR":
            if len(cmd) < WIDTH - 3:
                cmd += val
                send(val)
            else:
                bip()

    cursor(False)

# Websocket

def mp_key(code: str):
    return "%13" + code

def ws_log(*args):
    try:
        msg = "[WS] " + " ".join(str(a) for a in args)
        print(msg)
    except:
        pass

def ws_closed_screen():
    clear()
    header("WebSocket", bg=ROUGE, fg=BLANC)

    textbg(5, 1, "Connexion interrompue".ljust(WIDTH), ROUGE, BLANC)

    if WS_LAST_CLOSE_INFO:
        textbg(7, 1, str(WS_LAST_CLOSE_INFO)[:WIDTH].ljust(WIDTH), NOIR, BLANC)

    footer("SOMMAIRE pour retour", bg=ROUGE, fg=BLANC)

    while True:
        ev = read_event()
        if ev and ev[0] == "KEY" and ev[1] == KEY_SOMMAIRE:
            break

def ws_connect(url):
    global WS, WS_RUNNING, WS_URL, WS_STATE

    WS_URL = url
    WS_RUNNING = False
    WS_STATE = "CONNECTING"

    ws_log("Connecting to:", url)

    def on_message(ws, message):
        ws_log("RX message:", repr(message))

        try:
            if isinstance(message, str):
                _write_bytes(message.encode('latin-1', errors='ignore'))
            else:
                _write_bytes(message)
        except Exception as e:
            ws_log("DISPLAY ERROR:", e)

    def on_open(ws):
        global WS_STATE
        WS_STATE = "CONNECTED"
        ws_log("CONNECTED")


    def on_close(ws, code, msg=None):
        global WS_STATE, WS_LAST_CLOSE_INFO
        WS_STATE = "CLOSED"
        WS_LAST_CLOSE_INFO = f"{code} {msg}"
        ws_log("CLOSED", code, msg)


    def on_error(ws, err):
        global WS_STATE
        WS_STATE = "ERROR"
        ws_log("ERROR:", err)

    WS = websocket.WebSocketApp(
        url,
        header=[
            "User-Agent: {APP_UA}",
        ],
        on_message=on_message,
        on_open=on_open,
        on_close=on_close,
        on_error=on_error
    )

    def runner():
        try:
            ws_log("Thread started")
            WS.run_forever(
                ping_interval=0,
                ping_timeout=10
            )
        except Exception as e:
            ws_log("FATAL WS THREAD ERROR:", e)

    t = threading.Thread(target=runner, daemon=True)
    t.start()

def ws_send_raw(data: str):
    """
    Convertit %XX en octets.
    Uniquement pour le transport WebSocket.
    """
    i = 0
    out = bytearray()

    while i < len(data):
        if data[i] == '%' and i + 2 < len(data):
            try:
                out.append(int(data[i+1:i+3], 16))
                i += 3
                continue
            except:
                pass

        out.append(ord(data[i]))
        i += 1

    if WS:
        try:
            ws_log("TX RAW:", out)
            WS.send(out)
        except Exception as e:
            ws_log("SEND ERROR:", e)

def ws_send(data: str):
    global WS
    if not WS:
        ws_log("WS not connected")
        return
    try:
        ws_log("TX:", repr(data))
        WS.send(data)
    except Exception as e:
        ws_log("SEND ERROR:", e)

def ws_handle_input(event):
    et, val = event

    if et == "CHAR":
        ws_send(val)
        return

    if et != "KEY":
        return

    if val == KEY_ENVOI:
        ws_send_raw(mp_key("A"))   # ENVOI

    elif val == KEY_SUITE:
        ws_send_raw(mp_key("H"))   # Suite

    elif val == KEY_CORRECTION:
        ws_send_raw(mp_key("G"))   # Correction

    elif val == KEY_GUIDE:
        ws_send_raw(mp_key("D"))   # Guide

    elif val == KEY_REPETITION:
        ws_send_raw(mp_key("C"))   # Répétition

    elif val == KEY_RETOUR:
        ws_send_raw(mp_key("B"))   # Retour

    elif val == KEY_ANNULATION:
        ws_send_raw(mp_key("E"))   # Annulation

    elif val == KEY_SOMMAIRE:
        ws_send_raw(mp_key("F"))   # Sommaire

    elif val == KEY_CNXFIN:
        ws_send_raw(mp_key("I"))   # Connexion / Fin

def app_websocket():
    global WS, WS_RUNNING, WS_URL, WS_STATE
    clear()
    header('WebSocket', bg=MAGENTA, fg=BLANC)

    textbg(2, 1, 'URL WebSocket (wss:// ou ws://):', MAGENTA, BLANC)
    url, key = read_input(4, 3, 34, data="")

    if key == KEY_SOMMAIRE or not url:
        return

    if not (url.startswith("ws://") or url.startswith("wss://")):
        status("URL invalide", bg=ROUGE, fg=BLANC)
        return

    clear()
    header('WebSocket', bg=MAGENTA, fg=BLANC)
    textbg(2, 1, 'Connexion...'.ljust(WIDTH), MAGENTA, BLANC)

    ws_connect(url)

    footer('SOMMAIRE: retour', bg=MAGENTA, fg=BLANC)

    cnxfin_count = 0

    while True:
        ev = read_event()
        if not ev:
            continue

        et, val = ev

        if WS_STATE in ("CLOSED", "ERROR"):
            ws_closed_screen()
            break

        if et == "KEY":

            if val == KEY_CNXFIN:
                cnxfin_count += 1

                if cnxfin_count == 1:
                    ws_handle_input(ev)

                if cnxfin_count >= 4:
                    ws_log("Appuie sur CNX/FIN deux fois.")
                    try:
                        if WS:
                            WS.close()
                    except:
                        pass
                    WS_STATE = "CLOSED"
                    ws_closed_screen()
                    break
            else:
                cnxfin_count = 0
                ws_handle_input(ev)

        elif et == "CHAR":
            ws_handle_input(ev)

# Configuration

_CONFIG_ITEMS = [
    ('Wi-Fi',      None),
    ('Nom d\'hote',   None),
    ('Identifiants et SSH', None),
]

def app_config():
    cfg = load_config()

    options = [
        "Hostname",
        "Wi-Fi",
        "Mise à jour",
        "Infos système",
        "Redémarrer",
    ]

    selected = 0

    # Dessiner le menu.
    def draw_static():
        clear()
        header("Configuration", bg=ROUGE, fg=BLANC)
        info = f"{get_hostname()}  {get_ip()}  up {get_uptime()}"
        textbg(2, 1, info[:WIDTH].ljust(WIDTH), ROUGE, BLANC)
        pos(4, 3)
        color(CYAN)
        send("Paramètres disponibles :")
        color(BLANC)
        for i, opt in enumerate(options):
            y = 6 + i * 2
            textbg(y, 3, f"  {opt}".ljust(WIDTH - 3), NOIR, BLANC)
        footer("ENVOI: choisir  SUITE/RETOUR: nav",
               bg=ROUGE, fg=BLANC)

    # Uniquement modifier les lignes nécessaires.
    def update_row(i, active):
        y = 6 + i * 2
        label = options[i]
        if active:
            textbg(y, 3, f"> {label}".ljust(WIDTH - 3), ROUGE, JAUNE)
        else:
            textbg(y, 3, f"  {label}".ljust(WIDTH - 3), NOIR, BLANC)

    # Entrée de texte.
    def text_input(ligne, colonne, longueur, masked=False):
        """Read a line of text. Returns string or None if cancelled."""
        data = ""
        pos(ligne, colonne)
        fill('.', longueur)
        pos(ligne, colonne)
        cursor(True)
        while True:
            ev = read_event()
            if not ev:
                continue
            et, val = ev
            if et == "CHAR" and len(data) < longueur:
                data += val
                send('*' if masked else val)
            elif et == "KEY":
                if val == KEY_CORRECTION and data:
                    data = data[:-1]
                    pos(ligne, colonne)
                    fill('.', longueur)
                    pos(ligne, colonne)
                    send(('*' if masked else '') * len(data) if masked else data)
                    pos(ligne, colonne + len(data))
                elif val == KEY_ANNULATION:
                    data = ""
                    pos(ligne, colonne)
                    fill('.', longueur)
                    pos(ligne, colonne)
                elif val == KEY_ENVOI:
                    cursor(False)
                    return data if data.strip() else None
                elif val == KEY_SOMMAIRE:
                    cursor(False)
                    return None
            else:
                bip()

    # Changement du hostname.
    def edit_hostname():
        clear()
        header("Nom d'hôte", bg=JAUNE, fg=NOIR)
        textbg(2, 1, "Modifier le nom de la machine".ljust(WIDTH), JAUNE, NOIR)
        pos(5, 3)
        color(BLANC)
        send("Actuel : ")
        color(JAUNE)
        send(get_hostname())
        color(BLANC)
        pos(8, 3)
        send("Nouveau (ENVOI pour valider) :")
        new = text_input(10, 3, 30)
        if new:
            sys_run(f"hostnamectl set-hostname {new.strip()}")
            cfg["HOSTNAME"] = new.strip()
            save_config(cfg)
            status("Nom d'hote mis a jour !", delay=1.5)

    # Changement du Réseau Wi-Fi
    def edit_wifi():
        clear()
        header("Wi-Fi", bg=BLEU, fg=BLANC)
        textbg(2, 1, "Configuration Wi-Fi".ljust(WIDTH), BLEU, BLANC)

        # Afficher la connexion actuelle.
        current_ssid = sys_run(
            "nmcli -t -f active,ssid dev wifi | grep '^yes' | cut -d: -f2"
        ).strip()
        pos(4, 3)
        color(CYAN)
        send("Réseau actuel : ")
        color(BLANC)
        send(current_ssid if current_ssid else "Non connecté")

        pos(6, 3)
        color(BLANC)
        send("SSID :")
        ssid = text_input(7, 3, 32)
        if not ssid:
            return

        pos(9, 3)
        send("Mot de passe (ENVOI si vide) :")
        password = text_input(10, 3, 32, masked=True)

        # Tentative de connexion.
        clear()
        header("Wi-Fi", bg=BLEU, fg=BLANC)
        textbg(2, 1, "Connexion en cours...".ljust(WIDTH), BLEU, BLANC)
        pos(5, 3)
        color(CYAN)
        send("SSID : ")
        color(BLANC)
        send(ssid)
        cursor(False)

        ssid_escaped = ssid.replace('"', '\\"')
        password_escaped = password.replace('"', '\\"') if password else ""

        # Supprimer la connexion actuelle.
        sys_run(f'nmcli con delete "{ssid_escaped}" 2>/dev/null')

        # Créer la connexion.
        ## SSID.
        sys_run(
            f'nmcli con add type wifi ifname wlan0 con-name "{ssid_escaped}" ssid "{ssid_escaped}"'
        )

        ## Mot de passe.
        if password:
            sys_run(
                f'nmcli con modify "{ssid_escaped}" wifi-sec.key-mgmt wpa-psk'
            )
            sys_run(
                f'nmcli con modify "{ssid_escaped}" wifi-sec.psk "{password_escaped}"'
            )

        # Se connecter
        result = sys_run(f'nmcli con up "{ssid_escaped}"')

        ## Détection et affichage du résultat.
        success = "successfully" in result.lower()
        pos(7, 3)
        if success:
            color(VERT)
            send("Connexion réussite !")
            color(BLANC)
            pos(8, 3)
            send("IP : " + get_ip())
        else:
            color(ROUGE)
            send("Echec de connexion.")
            color(BLANC)
            # Afficher la première ligne de l'erreur.
            first_line = result.strip().splitlines()[0][:WIDTH - 3] if result.strip() else ""
            pos(8, 3)
            send(first_line)

        footer("SOMMAIRE pour retourner", bg=BLEU, fg=BLANC)
        while True:
            ev = read_event()
            if ev and ev[0] == "KEY" and ev[1] == KEY_SOMMAIRE:
                break

    # Mise à jour du système.
    def do_upgrade():

        clear()
        header("Mise à jour", bg=VERT, fg=NOIR)
        textbg(2, 1, "Préparation...".ljust(WIDTH), VERT, NOIR)
        cursor(False)

        output_line = 4

        def print_line(text):
            nonlocal output_line
            while len(text) > 0:
                if output_line > 23:
                    for l in range(4, 24):
                        textbg(l, 1, ' ' * WIDTH, NOIR, BLANC)
                    output_line = 4
                pos(output_line, 1)
                color(BLANC)
                send(text[:WIDTH])
                text = text[WIDTH:]
                output_line += 1

        # Télécharger la configuration JSON.
        try:
            textbg(2, 1, "Téléchargement de la config...".ljust(WIDTH), VERT, NOIR)
            with urllib.request.urlopen(
                "https://raw.githubusercontent.com/Mickmick21/MiniPi/refs/heads/main/latest.json",
                timeout=5
            ) as r:
                cfg = json.loads(r.read().decode())
        except Exception as e:
            textbg(2, 1, "Erreur lors du chargement de la config.".ljust(WIDTH), ROUGE, BLANC)
            print_line(str(e))
            footer("SOMMAIRE pour retour", bg=ROUGE, fg=BLANC)
            while True:
                ev = read_event()
                if ev and ev[0] == "KEY" and ev[1] == KEY_SOMMAIRE:
                    return

        # Mise à jour via APT
        if cfg.get("doAptBeforeUpdate", False):

            cmds = [
                ("apt update", "Mise à jour des listes..."),
                ("apt upgrade -y", "Installation..."),
                ("apt autoremove -y", "Nettoyage..."),
            ]

            for cmd, label in cmds:
                textbg(2, 1, label.ljust(WIDTH), VERT, NOIR)

                proc = subprocess.Popen(
                    cmd, shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                for raw_line in proc.stdout:
                    line = raw_line.decode('utf-8', errors='replace').rstrip()
                    if line:
                        print_line(line)

                proc.wait()

        # Télécharger et executer le script.
        ## TODO: Vérifier via une signature le script pour éviter une attaque MITM.
        script_url = cfg.get("updateScript")

        if script_url:
            textbg(2, 1, "Téléchargement du script...".ljust(WIDTH), VERT, NOIR)

            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".py")
                os.close(fd)

                urllib.request.urlretrieve(script_url, tmp_path)

                textbg(2, 1, "Execution du script...".ljust(WIDTH), VERT, NOIR)

                proc = subprocess.Popen(
                    f"python3 {tmp_path}",
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                )

                for raw_line in proc.stdout:
                    line = raw_line.decode('utf-8', errors='replace').rstrip()
                    if line:
                        print_line(line)

                proc.wait()

            except Exception as e:
                textbg(2, 1, "Erreur update script".ljust(WIDTH), ROUGE, BLANC)
                print_line(str(e))

        else:
            print_line("Pas de script fourni.")

        textbg(2, 1, "Mise à jour terminée !".ljust(WIDTH), VERT, NOIR)
        footer("SOMMAIRE pour retourner", bg=VERT, fg=NOIR)

        while True:
            ev = read_event()
            if ev and ev[0] == "KEY" and ev[1] == KEY_SOMMAIRE:
                break

    # Infos systèmes (Machine, pas du Minitel).
    def show_info():
        clear()
        header("Infos système", bg=CYAN, fg=NOIR)
        textbg(2, 1, "Etat de la machine".ljust(WIDTH), CYAN, NOIR)

        pos(4, 3);  color(CYAN);  send("Nom    : "); color(BLANC); send(get_hostname())
        pos(5, 3);  color(CYAN);  send("IP     : "); color(BLANC); send(get_ip())
        pos(6, 3);  color(CYAN);  send("Uptime : "); color(BLANC); send(get_uptime())
        pos(7, 3);  color(CYAN);  send("Arch   : "); color(BLANC); send(sys_run("uname -m").strip())

        # Température du Processeur
        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                temp = int(f.read().strip()) // 1000
            temp_str = f"{temp}°C"
        except Exception:
            temp_str = "?"
        pos(8, 3);  color(CYAN);  send("Temp   : "); color(BLANC); send(temp_str)

        # Utilisation du disque.
        disk_out = sys_run("df -h /").strip().splitlines()
        disk_cols = disk_out[-1].split() if disk_out else []
        disk = f"{disk_cols[2]}/{disk_cols[1]} ({disk_cols[4]})" if len(disk_cols) >= 5 else "?"
        pos(9, 3);  color(CYAN);  send("Disque : "); color(BLANC); send(disk)

        # RAM totale et libre.
        mem_out = sys_run("free -h").strip().splitlines()
        mem_cols = mem_out[1].split() if len(mem_out) > 1 else []
        mem = f"{mem_cols[2]}/{mem_cols[1]}" if len(mem_cols) >= 3 else "?"
        pos(10, 3); color(CYAN);  send("RAM    : "); color(BLANC); send(mem)

        # Version du script
        pos(11, 3); color(CYAN); send("Version: "); color(BLANC); send(APP_VERSION)

        color(BLANC)
        footer("SOMMAIRE pour retourner", bg=CYAN, fg=NOIR)
        while True:
            ev = read_event()
            if ev and ev[0] == "KEY" and ev[1] == KEY_SOMMAIRE:
                break

    # Dessiner le menu et les options.
    draw_static()
    for i in range(len(options)):
        update_row(i, i == selected)
    cursor(False)

    # Boucle principale.
    actions = [edit_hostname, edit_wifi, do_upgrade, show_info, None]

    while True:
        ev = read_event()
        if not ev:
            continue
        et, val = ev

        if et == "KEY":
            if val == KEY_SUITE:
                old = selected
                selected = (selected + 1) % len(options)
                update_row(old, False)
                update_row(selected, True)

            elif val == KEY_RETOUR:
                old = selected
                selected = (selected - 1) % len(options)
                update_row(old, False)
                update_row(selected, True)

            elif val == KEY_ENVOI:
                if selected == 4:  
                    # Redémarrer
                    status("Redemarrage...", bg=ROUGE, fg=BLANC, delay=1.0)
                    sys_run("reboot")
                elif actions[selected]:
                    actions[selected]()
                    draw_static()
                    for i in range(len(options)):
                        update_row(i, i == selected)

            elif val == KEY_SOMMAIRE:
                break

#  Menu principal.

_MENU_ITEMS = [
    ('1', 'Terminal shell',  app_shell),
    ('2', 'WebSocket',       app_websocket),
    ('3', 'Configuration',   app_config),
]

def _draw_menu_item(i: int, selected: bool):
    """Redraw a single menu entry row (padding line + label line)."""
    key, label, _ = _MENU_ITEMS[i]
    l = 5 + i * 3
    textbg(l,     1, ' ' * WIDTH, NOIR, BLANC)
    if selected:
        textbg(l + 1, 1, f'   > {key}. {label}'.ljust(WIDTH), CYAN, JAUNE)
    else:
        textbg(l + 1, 1, f'     {key}. {label}'.ljust(WIDTH), NOIR, BLANC)


def draw_menu(selected: int):
    """Full screen draw — only called on first entry or after returning from an app."""
    clear()
    header(f'{APP_NAME} v{APP_VERSION}', bg=BLEU, fg=BLANC)
    info = f'{get_hostname()}  {get_ip()}  up {get_uptime()}'
    textbg(2, 1, info[:WIDTH].ljust(WIDTH), BLEU, CYAN)
    for i in range(len(_MENU_ITEMS)):
        _draw_menu_item(i, i == selected)
    footer('SUITE/RETOUR: nav  ENVOI: lancer', bg=BLEU, fg=BLANC)
    cursor(False)


def disable_local_echo():
    """
    Désactiver l'echo local:
    ESC 0x3B (PRO3) + 0x60 (P_OFF) + 0x5A (MODEM_RX) + 0x51 (CLAVIER_TX)
    """
    ser.write(b'\x1b\x3b\x60\x5a\x51')
    time.sleep(0.1)

def background_init():
    """
    Envoyer toutes les 5 secondes les commandes d'initialisations.
    """
    while True:
        disable_local_echo()


        time.sleep(5)

def main():
    selected = 0
    draw_menu(selected)

    while True:
        ev = read_event()
        if ev is None:
            continue
        et, val = ev

        if et == "KEY":
            if val == KEY_SUITE:
                prev = selected
                selected = (selected + 1) % len(_MENU_ITEMS)
                _draw_menu_item(prev, False)
                _draw_menu_item(selected, True)
            elif val == KEY_RETOUR:
                prev = selected
                selected = (selected - 1) % len(_MENU_ITEMS)
                _draw_menu_item(prev, False)
                _draw_menu_item(selected, True)
            elif val == KEY_ENVOI:
                _MENU_ITEMS[selected][2]()
                draw_menu(selected)
            elif val == KEY_ANNULATION:
                draw_menu(selected)

        elif et == "CHAR":
            for i, (key, _, fn) in enumerate(_MENU_ITEMS):
                if val == key:
                    if i != selected:
                        prev = selected
                        selected = i
                        _draw_menu_item(prev, False)
                        _draw_menu_item(selected, True)
                    fn()
                    draw_menu(selected)
                    break

def fatal_error(titre: str, erreur: str, description: str, actions: list):
    """
    Affiche un écran d'erreur bloquant.

    actions:
        - "Redémarrer le système"
        - "Redémarrer le script"
        - "Continuer"
        - "Retour au menu principal"
    """

    selected = 0

    def draw():
        clear()
        header(titre[:WIDTH], bg=ROUGE, fg=BLANC)

        # Erreur.
        textbg(3, 1, erreur[:WIDTH].ljust(WIDTH), ROUGE, BLANC)

        # Description.
        lines = []
        desc = description
        while desc:
            lines.append(desc[:WIDTH])
            desc = desc[WIDTH:]

        for i, line in enumerate(lines[:5]):
            textbg(5 + i, 1, line.ljust(WIDTH), NOIR, BLANC)

        # Actions.
        for i, act in enumerate(actions):
            y = 12 + i * 2
            if i == selected:
                textbg(y, 3, f"> {act}".ljust(WIDTH - 3), ROUGE, JAUNE)
            else:
                textbg(y, 3, f"  {act}".ljust(WIDTH - 3), NOIR, BLANC)

        footer("SUITE/RETOUR: choix  ENVOI: valider", bg=ROUGE, fg=BLANC)
        cursor(False)

    def update_row(i, active):
        y = 12 + i * 2
        act = actions[i]
        if active:
            textbg(y, 3, f"> {act}".ljust(WIDTH - 3), ROUGE, JAUNE)
        else:
            textbg(y, 3, f"  {act}".ljust(WIDTH - 3), NOIR, BLANC)

    draw()

    while True:
        ev = read_event()
        if not ev:
            continue

        et, val = ev

        if et == "KEY":

            if val == KEY_SUITE:
                old = selected
                selected = (selected + 1) % len(actions)
                update_row(old, False)
                update_row(selected, True)

            elif val == KEY_RETOUR:
                old = selected
                selected = (selected - 1) % len(actions)
                update_row(old, False)
                update_row(selected, True)

            elif val == KEY_ENVOI:
                return actions[selected]

            elif val == KEY_SOMMAIRE:
                return "Retour au menu principal"


if __name__ == '__main__':
    action = None

    try:
        main()

    except (KeyboardInterrupt, SystemExit):
        cursor(True)
        clear()
        print("Arrêt du script.")
        raise

    except MemoryError as e:
        action = fatal_error(
            "ERREUR FATALE",
            "Mémoire insuffisante.",
            str(e),
            ["Redémarrer le système", "Redémarrer le script", "Retour au menu principal"]
        )

    except SyntaxError as e:
        action = fatal_error(
            "ERREUR FATALE",
            "Erreur interne, signalez ce bug au dev. (SyntaxError)",
            str(e),
            ["Redémarrer le script", "Retour au menu principal"]
        )

    except NameError as e:
        action = fatal_error(
            "ERREUR FATALE",
            "Erreur interne, signalez ce bug au dev. (NameError)",
            str(e),
            ["Redémarrer le script", "Retour au menu principal"]
        )

    except TypeError as e:
        action = fatal_error(
            "ERREUR FATALE",
            "Erreur interne, signalez ce bug au dev. (TypeError)",
            str(e),
            ["Redémarrer le script", "Retour au menu principal"]
        )

    except ValueError as e:
        action = fatal_error(
            "ERREUR FATALE",
            "Erreur interne, signalez ce bug au dev. (ValueError)",
            str(e),
            ["Redémarrer le script", "Retour au menu principal"]
        )

    except AttributeError as e:
        action = fatal_error(
            "ERREUR FATALE",
            "Erreur interne, signalez ce bug au dev. (AttributeError)",
            str(e),
            ["Redémarrer le script", "Retour au menu principal"]
        )

    except (OSError, ConnectionError, TimeoutError, PermissionError) as e:
        action = fatal_error(
            "ERREUR SYSTEME",
            type(e).__name__,
            str(e),
            ["Continuer", "Retour menu principal"]
        )

    except Exception as e:
        action = fatal_error(
            "ERREUR FATALE",
            type(e).__name__,
            str(e),
            ["Continuer", "Redémarrer le script", "Retour au menu principal"]
        )

    # Actions.
    if action == "Redémarrer le système":
        sys_run("reboot")

    elif action == "Redémarrer le script":
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif action == "Continuer":
        #TODO: Faire que ça continue au lieu de retourner au menu principal.
        main()

    elif action == "Retour au menu principal":
        main()
