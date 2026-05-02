#!/usr/bin/env python3
"""
MiniPi - Minitel interface for Raspberry Pi
GPIO UART (ttyAMA0) @ 1200 baud, 7E1
(C) 2026 Mickmick.bin - GNU General Public License v3.0
License available at https://choosealicense.com/licenses/gpl-3.0/
"""

import time
import subprocess
import socket
import os
import urllib.request
import json
import tempfile
import threading
import re
import pty
import select
import sys
import serial
import websocket

ANSI_RE = re.compile(r"\x1b\[([0-9;]*)m")

WS = None
WS_STATE = "DISCONNECTED"  # DISCONNECTED | CONNECTING | CONNECTED | CLOSED | ERROR
WS_LAST_CLOSE_INFO = ""

APP_NAME = "MiniPi"
APP_VERSION = "1.1.0"
APP_UA = f"{APP_NAME}/{APP_VERSION}"

# Initialiser le serial.

ser = serial.Serial(
    "/dev/ttyAMA0", baudrate=1200, bytesize=7, parity="E", stopbits=1, timeout=0.1
)

# Couleurs.

# Vitesses disponibles pour la négociation de vitesse.
VITESSES = [75, 300, 1200, 4800]


def _build_exchange_byte(tx_baud: int, rx_baud: int) -> int:
    """
    Construit le byte d'échange de vitesse selon STUM 1B section 8.1.
    Format : P|1|E2|E1|E0|R2|R1|R0
      - Bit 7 (P)  : bit de parité paire sur les bits 6-0
      - Bit 6      : toujours 1
      - Bits 5-3 (E) : vitesse d'émission
      - Bits 2-0 (R) : vitesse de réception
    Codes vitesse : 001=75bd, 010=300bd, 100=1200bd, 110=4800bd, 111=9600bd
    """
    codes = {75: 0b001, 300: 0b010, 1200: 0b100, 4800: 0b110, 9600: 0b111}
    e = codes[tx_baud]
    r = codes[rx_baud]
    # Bit 6 toujours 1, E sur bits 5-3, R sur bits 2-0
    val = (1 << 6) | (e << 3) | r
    # Parité paire : si le nombre de 1 dans les bits 6-0 est pair, mettre bit 7
    if bin(val).count("1") % 2 == 0:
        val |= 1 << 7
    return val


def set_baudrate(new_baud: int) -> bool:
    """
    Négocie un changement de vitesse avec le Minitel selon STUM 1B section 8.1.
    1. Envoie PRO1 + 0x6B + byte d'échange
    2. Attend la réponse PRO2 + 0x75 + byte d'échange
    3. Si le byte reçu correspond : change la vitesse et retourne True
    4. Sinon : ne change rien et retourne False
    """
    exchange_byte = _build_exchange_byte(new_baud, new_baud)

    # Envoi de la demande : PRO1 (ESC 0x39) + 0x6B + byte d'échange
    ser.write(bytes([0x1B, 0x39, 0x6B, exchange_byte]))

    # Attente de la réponse du Minitel (délai généreux : 500 ms)
    ser.timeout = 0.5
    try:
        resp = ser.read(4)
    except Exception:
        resp = b""
    finally:
        ser.timeout = 0.1

    # Réponse attendue : PRO2 (ESC 0x3A) + 0x75 + byte d'échange
    if (
        len(resp) >= 4
        and resp[0] == 0x1B
        and resp[1] == 0x3A
        and resp[2] == 0x75
        and resp[3] == exchange_byte
    ):
        # Le Minitel a accepté - on change la vitesse côté Pi
        ser.baudrate = new_baud
        return True

    # Refus ou réponse inattendue
    return False


NOIR = 0
ROUGE = 1
VERT = 2
JAUNE = 3
BLEU = 4
MAGENTA = 5
CYAN = 6
BLANC = 7


def ansi_apply(text: str, draw_char):
    """
    ansi_to_minitel mais en streaming.
    """

    fg = BLANC
    bg = NOIR

    i = 0
    while i < len(text):

        if text[i] == "\x1b" and i + 1 < len(text) and text[i + 1] == "[":
            j = i + 2
            seq = ""

            while j < len(text) and text[j] != "m":
                seq += text[j]
                j += 1

            if j < len(text):
                codes = seq.split(";")

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

        for code in codes.split(";"):
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

    return "".join(out)


# Codes de touches fonctions. (SEP + chr(64+code))

KEY_ENVOI = 1
KEY_RETOUR = 2
KEY_REPETITION = 3
KEY_GUIDE = 4
KEY_ANNULATION = 5
KEY_SOMMAIRE = 6
KEY_CORRECTION = 7
KEY_SUITE = 8
KEY_CNXFIN = 25

# Fonctions systèmes.

CONFIG_FILE = "/etc/minipi.conf"


def load_config():
    """Charger la configuration."""
    cfg = {}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return cfg


def save_config(cfg):
    """Sauvegarder la configuration."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        for k, v in cfg.items():
            f.write(f"{k}={v}\n")


def sys_run(cmd):
    """Executer une commande."""
    try:
        return subprocess.check_output(
            cmd, shell=True, stderr=subprocess.STDOUT
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
    """Envoyer des données brutes."""
    ser.write(data)


def send(text: str):
    """Envoyer du texte, avec convertition des accents."""
    text = _accents(text)
    _write_bytes(text.encode("latin-1", errors="ignore"))


def sendchr(code: int):
    """Envoyer un charactère."""
    _write_bytes(bytes([code]))


def sendesc(seq: str):
    """Envoyer le symbole ESC."""
    sendchr(27)
    send(seq)


# Contrôle de l'écran.


def clear():
    """Effacer l'écran et bouger le curseur à sa maison."""
    sendchr(12)  # FF


def pos(ligne: int, colonne: int = 1):
    """Positioner le curseur (ligne, colonne), 1-indexed."""
    if ligne == 1 and colonne == 1:
        sendchr(30)  # RS = home
    else:
        sendchr(31)  # US
        sendchr(64 + ligne)
        sendchr(64 + colonne)


def cursor(visible: bool):
    """Afficher ou cacher le curseur."""
    sendchr(17 if visible else 20)


def color(c: int):
    """Changer la couleur d'avant-plan."""
    sendesc(chr(64 + c))


def bgcolor(c: int):
    """Changer la couleur d'arrière-plan"""
    sendesc(chr(80 + c))


def inverse(on: bool = True):
    """Inverser."""
    sendesc("\x5d" if on else "\x5c")


def blink(on: bool = True):
    """Clignotement."""
    sendesc("\x48" if on else "\x49")


def underline(on: bool = True):
    """Soulignement."""
    sendesc(chr(90) if on else chr(89))


def bip():
    """Envoyer le charactère BELL."""
    sendchr(7)


def eol(ligne: int, colonne: int = 1):
    """Effacer de (ligne, colonne) à la fin de la ligne."""
    pos(ligne, colonne)
    sendchr(24)  # CAN


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
        sendchr(18)  # REP
        sendchr(64 + n)
        remaining -= n


def _accents(text: str) -> str:
    """Conversion des accents, STUM 2.3.1 p.22"""
    replacements = [
        ("à", "\x19\x41a"),
        ("â", "\x19\x43a"),
        ("ä", "\x19\x48a"),
        ("è", "\x19\x41e"),
        ("é", "\x19\x42e"),
        ("ê", "\x19\x43e"),
        ("ë", "\x19\x48e"),
        ("î", "\x19\x43i"),
        ("ï", "\x19\x48i"),
        ("ô", "\x19\x43o"),
        ("ö", "\x19\x48o"),
        ("ù", "\x19\x41u"),
        ("û", "\x19\x43u"),
        ("ü", "\x19\x48u"),
        ("ç", "\x19\x4bc"),
        ("À", "\x19\x41A"),
        ("È", "\x19\x41E"),
        ("É", "\x19\x42E"),
        ("Î", "\x19\x43I"),
        ("Ô", "\x19\x43O"),
        ("Ù", "\x19\x41U"),
        ("Ç", "\x19\x4bC"),
        ("£", "\x19\x23"),
        ("°", "\x19\x30"),
        ("¼", "\x19\x3c"),
        ("½", "\x19\x3d"),
        ("¾", "\x19\x3e"),
        ("←", "\x19\x2c"),
        ("↑", "\x19\x2d"),
        ("→", "\x19\x2e"),
        ("↓", "\x19\x2f"),
        ("Œ", "\x19\x6a"),
        ("œ", "\x19\x7a"),
    ]

    for src, dst in replacements:
        text = text.replace(src, dst)

    return text


# Entrée


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
        print(f"[RAW] {raw.hex()} ({list(raw)})")
        print(f"[EV ] {ev}")

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

    ch = c.decode("latin-1")
    if ch >= " ":
        ev = ("CHAR", ch)
        debug(ev, raw)
        return ev

    debug(None, raw)
    return None


def read_input(
    ligne: int, colonne: int, longueur: int, data: str = "", char_fill: str = "."
) -> tuple:
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
                data = ""
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


# Interface utilisateur

WIDTH = 40


def textbg(ligne: int, colonne: int, text: str, bg: int, fg: int = BLANC):
    """Écrire du texte avec une couleur de fond sur une seule ligne."""
    pos(ligne, colonne)
    sendesc(chr(80 + bg))
    sendesc(chr(64 + fg))
    send(" ")
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
    textbg(24, 1, padded[: WIDTH - 1], bg, fg)


def status(
    message: str, ligne: int = 23, bg: int = JAUNE, fg: int = NOIR, delay: float = 1.5
):
    """Afficher temporairement un message d'état coloré, puis l'effacer."""
    cursor(False)
    padded = message.center(WIDTH)
    textbg(ligne, 1, padded[:WIDTH], bg, fg)
    ser.flush()
    if delay != -1:
        time.sleep(delay)
        textbg(ligne, 1, " " * WIDTH, NOIR, BLANC)


def box(top: int, left: int, height: int, width: int, bg: int = NOIR, fg: int = BLANC):
    """Remplire une zone rectangulaire avec une couleur de fond."""
    for l in range(top, top + height):
        textbg(l, left, " " * width, bg, fg)


# Infos système minifiés (utilisé par Accueil et Configuration)


def get_hostname() -> str:
    """Retourne le hostname."""
    return socket.gethostname()


def get_ip() -> str:
    """Retourne l'adresse IP du Rapberry Pi."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except OSError:
        return "Pas d'internet"


def get_uptime() -> str:
    """Retourne le temps que le Raspberry Pi est allumé."""
    with open("/proc/uptime", encoding="utf-8") as f:
        secs = float(f.read().split()[0])
    h, m = divmod(int(secs) // 60, 60)
    return f"{h}h{m:02d}m"


# Terminal


def app_shell():
    """Application shell."""
    clear()
    header("Terminal", bg=VERT, fg=NOIR)
    textbg(2, 1, "SOMMAIRE: quitter  SUITE/RETOUR: historique".ljust(WIDTH), VERT, NOIR)

    cmd = ""
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
            header("Terminal", bg=VERT, fg=NOIR)
        color(VERT)
        pos(output_line, 1)
        send("$ ")
        color(BLANC)

    def redraw_cmd():
        color(VERT)
        pos(output_line, 1)
        send("$ ")
        color(BLANC)
        send(cmd[: WIDTH - 2].ljust(WIDTH - 2))
        pos(output_line, 3 + len(cmd))

    def term_write(data):
        nonlocal cursor_x, cursor_y

        def draw(ch, fg, bg):
            nonlocal cursor_x, cursor_y

            color(fg)
            bgcolor(bg)

            if ch == "\n":
                cursor_x = 0
                cursor_y += 1
                return

            if ch == "\r":
                cursor_x = 0
                return

            if ch == "\x08":
                if cursor_x > 0:
                    cursor_x -= 1
                    pos(cursor_y, cursor_x + 1)
                    send(" ")
                    pos(cursor_y, cursor_x + 1)
                return

            if cursor_x >= WIDTH:
                cursor_x = 0
                cursor_y += 1

            if cursor_y > 22:
                for l in range(4, 23):
                    textbg(l, 1, " " * WIDTH, NOIR, BLANC)
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
            keymap = {
                KEY_ENVOI: b"\n",
                KEY_CORRECTION: b"\x7f",
                KEY_ANNULATION: b"\x03",
                KEY_SOMMAIRE: b"\x04",
            }
            return keymap.get(val)

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
                send("\r\n")
                output_line += 1
                if cmd.strip():
                    history.append(cmd)
                    hist_idx = len(history)

                if cmd.strip() in ("exit", "quit"):
                    break

                cursor(False)

                def print_stream(text):
                    nonlocal output_line

                    text = ansi_to_minitel(text)

                    for line in text.splitlines():
                        if output_line > 22:
                            output_line = 4
                            clear()
                            header("Terminal", bg=VERT, fg=NOIR)

                        pos(output_line, 1)
                        send(line[:WIDTH])
                        output_line += 1

                try:
                    cursor_y = output_line
                    cursor_x = 0
                    sys_run_interactive(cmd, term_write, input_cb)
                    output_line = cursor_y + 1
                except Exception as e:
                    print_stream(f"[Erreur: {e}]")

                cmd = ""
                prompt_line()
                cursor(True)

            elif val == KEY_CORRECTION:
                if cmd:
                    cmd = cmd[:-1]
                    redraw_cmd()

            elif val == KEY_ANNULATION:
                cmd = ""
                redraw_cmd()

            elif val == KEY_SUITE:  # next history
                if history and hist_idx < len(history) - 1:
                    hist_idx += 1
                    cmd = history[hist_idx]
                    redraw_cmd()

            elif val == KEY_RETOUR:  # previous history
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
    """Touche fonction (Guide, ENVOI, Suite...)."""
    return "%13" + code


def ws_log(*args):
    """Fonction log WebSocket."""
    msg = "[WS] " + " ".join(str(a) for a in args)
    print(msg)


def ws_closed_screen():
    """Écran Connexion interrompue."""
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
    """Connexion à un serveur WebSocket Minitel."""
    global WS, WS_STATE

    WS_STATE = "CONNECTING"

    ws_log("Connecting to:", url)

    def on_message(_, message):
        ws_log("RX message:", repr(message))

        if isinstance(message, str):
            _write_bytes(message.encode("latin-1", errors="ignore"))
        else:
            _write_bytes(message)

    def on_open(_):
        global WS_STATE
        WS_STATE = "CONNECTED"
        ws_log("CONNECTED")

    def on_close(_, code, msg=None):
        global WS_STATE, WS_LAST_CLOSE_INFO
        WS_STATE = "CLOSED"
        WS_LAST_CLOSE_INFO = f"{code} {msg}"
        ws_log("CLOSED", code, msg)

    def on_error(_, err):
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
        on_error=on_error,
    )

    def runner():
        ws_log("Thread started")
        WS.run_forever(ping_interval=0, ping_timeout=10)

    ws_thread = threading.Thread(target=runner, daemon=True)
    ws_thread.start()


def ws_send_raw(data: str):
    """Convertit %XX en octets et envoi."""
    i = 0
    out = bytearray()

    while i < len(data):
        if data[i] == "%" and i + 2 < len(data):
            out.append(int(data[i + 1 : i + 3], 16))
            i += 3
            continue

        out.append(ord(data[i]))
        i += 1

    if WS:
        ws_log("TX RAW:", out)
        WS.send(out)


def ws_send(data: str):
    """Envoi de données texte."""
    global WS
    if not WS:
        ws_log("WS not connected")
        return
    ws_log("TX:", repr(data))
    WS.send(data)


_WS_KEY_MAP = {
    KEY_ENVOI: "A",
    KEY_SUITE: "H",
    KEY_CORRECTION: "G",
    KEY_GUIDE: "D",
    KEY_REPETITION: "C",
    KEY_RETOUR: "B",
    KEY_ANNULATION: "E",
    KEY_SOMMAIRE: "F",
    KEY_CNXFIN: "I",
}


def ws_handle_input(event):
    """Détecter et envoyer les touches."""
    et, val = event

    if et == "CHAR":
        ws_send(val)
        return

    if et != "KEY":
        return

    letter = _WS_KEY_MAP.get(val)
    if letter:
        ws_send_raw(mp_key(letter))


def app_websocket():
    """Application Websocket."""
    global WS, WS_STATE
    clear()
    header("WebSocket", bg=MAGENTA, fg=BLANC)

    textbg(2, 1, "URL WebSocket (wss:// ou ws://):", MAGENTA, BLANC)
    url, key = read_input(4, 3, 34, data="")

    if key == KEY_SOMMAIRE or not url:
        return

    if not (url.startswith("ws://") or url.startswith("wss://")):
        status("URL invalide", bg=ROUGE, fg=BLANC)
        return

    clear()
    header("WebSocket", bg=MAGENTA, fg=BLANC)
    textbg(2, 1, "Connexion...".ljust(WIDTH), MAGENTA, BLANC)

    ws_connect(url)

    footer("SOMMAIRE: retour", bg=MAGENTA, fg=BLANC)

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
                    if WS:
                        WS.close()
                    WS_STATE = "CLOSED"
                    ws_closed_screen()
                    break
            else:
                cnxfin_count = 0
                ws_handle_input(ev)

        elif et == "CHAR":
            ws_handle_input(ev)


# Configuration


def app_config():
    """Application Configuration."""
    cfg = load_config()

    options = [
        "Nom d'hôte",
        "Wi-Fi",
        "Vitesse",
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
        footer("ENVOI: choisir  SUITE/RETOUR: nav", bg=ROUGE, fg=BLANC)

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
        """Champ de texte, retourne un string ou None si annulé"""
        data = ""
        pos(ligne, colonne)
        fill(".", longueur)
        pos(ligne, colonne)
        cursor(True)
        while True:
            ev = read_event()
            if not ev:
                continue
            et, val = ev
            if et == "CHAR" and len(data) < longueur:
                data += val
                send("*" if masked else val)
            elif et == "KEY":
                if val == KEY_CORRECTION and data:
                    data = data[:-1]
                    pos(ligne, colonne)
                    fill(".", longueur)
                    pos(ligne, colonne)
                    send(("*" if masked else "") * len(data) if masked else data)
                    pos(ligne, colonne + len(data))
                elif val == KEY_ANNULATION:
                    data = ""
                    pos(ligne, colonne)
                    fill(".", longueur)
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
        textbg(2, 1, "Modifier le nom d'hôte".ljust(WIDTH), JAUNE, NOIR)
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
            status("Nom d'hôte mis a jour !", delay=1.5)

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
            sys_run(f'nmcli con modify "{ssid_escaped}" wifi-sec.key-mgmt wpa-psk')
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
            first_line = (
                result.strip().splitlines()[0][: WIDTH - 3] if result.strip() else ""
            )
            pos(8, 3)
            send(first_line)

        footer("SOMMAIRE pour retourner", bg=BLEU, fg=BLANC)
        while True:
            ev = read_event()
            if ev and ev[0] == "KEY" and ev[1] == KEY_SOMMAIRE:
                break

    def do_upgrade():
        """Mise à jour système."""

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
                        textbg(l, 1, " " * WIDTH, NOIR, BLANC)
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
                timeout=5,
            ) as r:
                cfg = json.loads(r.read().decode())
        except Exception as e:
            textbg(
                2,
                1,
                "Erreur lors du chargement de la config.".ljust(WIDTH),
                ROUGE,
                BLANC,
            )
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

                with subprocess.Popen(
                    cmd,
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                ) as proc:
                    for raw_line in proc.stdout:
                        line = raw_line.decode("utf-8", errors="replace").rstrip()
                        if line:
                            print_line(line)

        # Télécharger et executer le script.
        ## TODO: Vérifier via une signature le script pour éviter une attaque MITM.
        script_url = cfg.get("updateScript")

        if script_url:
            textbg(2, 1, "Téléchargement du script...".ljust(WIDTH), VERT, NOIR)

            try:
                fd, tmp_path = tempfile.mkstemp(suffix=".py")
                try:
                    os.close(fd)

                    urllib.request.urlretrieve(script_url, tmp_path)

                    textbg(2, 1, "Execution du script...".ljust(WIDTH), VERT, NOIR)

                    with subprocess.Popen(
                        f"python3 {tmp_path}",
                        shell=True,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT,
                    ) as proc:
                        for raw_line in proc.stdout:
                            line = raw_line.decode("utf-8", errors="replace").rstrip()
                            if line:
                                print_line(line)
                finally:
                    os.unlink(tmp_path)

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

    def show_info():
        """Infos systèmes."""
        clear()
        header("Infos système", bg=CYAN, fg=NOIR)
        textbg(2, 1, "Etat de la machine".ljust(WIDTH), CYAN, NOIR)

        pos(4, 3)
        color(CYAN)
        send("Nom    : ")
        color(BLANC)
        send(get_hostname())
        pos(5, 3)
        color(CYAN)
        send("IP     : ")
        color(BLANC)
        send(get_ip())
        pos(6, 3)
        color(CYAN)
        send("Uptime : ")
        color(BLANC)
        send(get_uptime())
        pos(7, 3)
        color(CYAN)
        send("Arch   : ")
        color(BLANC)
        send(sys_run("uname -m").strip())

        # Température du Processeur
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", encoding="utf-8") as f:
                temp = int(f.read().strip()) // 1000
            temp_str = f"{temp}°C"
        except Exception:
            temp_str = "?"
        pos(8, 3)
        color(CYAN)
        send("Temp   : ")
        color(BLANC)
        send(temp_str)

        # Utilisation du disque.
        disk_out = sys_run("df -h /").strip().splitlines()
        disk_cols = disk_out[-1].split() if disk_out else []
        disk = (
            f"{disk_cols[2]}/{disk_cols[1]} ({disk_cols[4]})"
            if len(disk_cols) >= 5
            else "?"
        )
        pos(9, 3)
        color(CYAN)
        send("Disque : ")
        color(BLANC)
        send(disk)

        # RAM totale et libre.
        mem_out = sys_run("free -h").strip().splitlines()
        mem_cols = mem_out[1].split() if len(mem_out) > 1 else []
        mem = f"{mem_cols[2]}/{mem_cols[1]}" if len(mem_cols) >= 3 else "?"
        pos(10, 3)
        color(CYAN)
        send("RAM    : ")
        color(BLANC)
        send(mem)

        # Version du script
        pos(11, 3)
        color(CYAN)
        send("Version: ")
        color(BLANC)
        send(APP_VERSION)

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

    # Vitesse baudrate
    def edit_vitesse():
        """Négociation de vitesse avec le Minitel."""
        clear()
        header("Vitesse de connexion", bg=BLEU, fg=BLANC)
        textbg(2, 1, "Choisir la vitesse de transmission".ljust(WIDTH), BLEU, BLANC)

        vitesses = [75, 300, 1200, 4800]
        sel_v = vitesses.index(ser.baudrate) if ser.baudrate in vitesses else 2
        labels = [f"{v} bauds" for v in vitesses]

        def draw_vitesse_row(i, active):
            y = 5 + i * 2
            if active:
                textbg(y, 5, f"> {labels[i]}".ljust(WIDTH - 5), BLEU, JAUNE)
            else:
                textbg(y, 5, f"  {labels[i]}".ljust(WIDTH - 5), NOIR, BLANC)

        # Affichage initial
        for i in range(len(vitesses)):
            draw_vitesse_row(i, i == sel_v)

        pos(14, 3)
        color(CYAN)
        send(f"Vitesse actuelle : {ser.baudrate} bd")
        color(BLANC)

        footer("ENVOI: choisir  SUITE/RETOUR: nav", bg=BLEU, fg=BLANC)
        cursor(False)

        while True:
            ev = read_event()
            if not ev:
                continue
            et, val = ev

            if et == "KEY":
                if val == KEY_SUITE:
                    old_v = sel_v
                    sel_v = (sel_v + 1) % len(vitesses)
                    draw_vitesse_row(old_v, False)
                    draw_vitesse_row(sel_v, True)

                elif val == KEY_RETOUR:
                    old_v = sel_v
                    sel_v = (sel_v - 1) % len(vitesses)
                    draw_vitesse_row(old_v, False)
                    draw_vitesse_row(sel_v, True)

                elif val == KEY_ENVOI:
                    cible = vitesses[sel_v]

                    if cible == ser.baudrate:
                        # Déjà à cette vitesse
                        status("Déjà à cette vitesse.", delay=1.2)
                        break

                    # Afficher "négociation en cours"
                    pos(16, 3)
                    color(CYAN)
                    send(f"Négociation {cible} bd...".ljust(WIDTH - 3))
                    color(BLANC)

                    if set_baudrate(cible):
                        pos(17, 3)
                        color(VERT)
                        send(f"Accepté ! Vitesse : {cible} bd".ljust(WIDTH - 3))
                        color(BLANC)
                        # Mettre à jour l'affichage vitesse actuelle
                        pos(14, 3)
                        color(CYAN)
                        send(f"Vitesse actuelle : {ser.baudrate} bd")
                        color(BLANC)
                    else:
                        pos(17, 3)
                        color(ROUGE)
                        send("Refusé par le Minitel.".ljust(WIDTH - 3))
                        color(BLANC)

                    footer("SOMMAIRE: retour", bg=BLEU, fg=BLANC)
                    # Attendre SOMMAIRE pour revenir
                    while True:
                        ev2 = read_event()
                        if ev2 and ev2[0] == "KEY" and ev2[1] == KEY_SOMMAIRE:
                            break
                    break

                elif val == KEY_SOMMAIRE:
                    break

    # Boucle principale.
    actions = [edit_hostname, edit_wifi, edit_vitesse, do_upgrade, show_info, None]

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
                if selected == 5:
                    # Redémarrer
                    clear()
                    status("Redémarrage...", bg=ROUGE, fg=BLANC, delay=-1)
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
    ("1", "Terminal shell", app_shell),
    ("2", "WebSocket", app_websocket),
    ("3", "Configuration", app_config),
]


def _draw_menu_item(i: int, selected: bool):
    """Retracer une seule option."""
    key, label, _ = _MENU_ITEMS[i]
    l = 5 + i * 3
    textbg(l, 1, " " * WIDTH, NOIR, BLANC)
    if selected:
        textbg(l + 1, 1, f"   > {key}. {label}".ljust(WIDTH), CYAN, JAUNE)
    else:
        textbg(l + 1, 1, f"     {key}. {label}".ljust(WIDTH), NOIR, BLANC)


def draw_menu(selected: int):
    """Retracage complet."""
    clear()
    header(f"{APP_NAME} v{APP_VERSION}", bg=BLEU, fg=BLANC)
    info = f"{get_hostname()}  {get_ip()}  up {get_uptime()}"
    textbg(2, 1, info[:WIDTH].ljust(WIDTH), BLEU, CYAN)
    for i in range(len(_MENU_ITEMS)):
        _draw_menu_item(i, i == selected)
    footer("SUITE/RETOUR: nav  ENVOI: lancer", bg=BLEU, fg=BLANC)
    cursor(False)


def disable_local_echo():
    """
    Désactiver l'echo local:
    ESC 0x3B (PRO3) + 0x60 (P_OFF) + 0x5A (MODEM_RX) + 0x51 (CLAVIER_TX)
    """
    ser.write(b"\x1b\x3b\x60\x5a\x51")
    time.sleep(0.1)


def background_init():
    """Envoyer toutes les 5 secondes les commandes d'initialisations."""
    while True:
        disable_local_echo()

        time.sleep(5)


def main():
    """Menu principal"""
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


if __name__ == "__main__":
    action = None

    try:
        t = threading.Thread(target=background_init, daemon=True)
        t.start()
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
            [
                "Redémarrer le système",
                "Redémarrer le script",
                "Retour au menu principal",
            ],
        )

    except (AttributeError, ValueError, TypeError, NameError, SyntaxError) as e:
        action = fatal_error(
            "ERREUR FATALE",
            f"Erreur interne, signalez ce bug au dev. ({type(e).__name__})",
            str(e),
            ["Redémarrer le script", "Retour au menu principal"],
        )

    except (OSError, ConnectionError, TimeoutError, PermissionError) as e:
        action = fatal_error(
            "ERREUR SYSTEME",
            type(e).__name__,
            str(e),
            ["Continuer", "Retour menu principal"],
        )

    # Actions.
    if action == "Redémarrer le système":
        sys_run("reboot")

    elif action == "Redémarrer le script":
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif action == "Continuer":
        # TODO: Faire que ça continue au lieu de retourner au menu principal.
        main()

    elif action == "Retour au menu principal":
        main()
