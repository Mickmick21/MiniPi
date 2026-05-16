#!/usr/bin/env python3

import json
import os
import subprocess
import tempfile
import time
import urllib.request
import re
import serial

APP_VERSION = "setup"

WIDTH = 40

# Couleurs Vidéotex
NOIR = 0
ROUGE = 1
VERT = 2
JAUNE = 3
BLEU = 4
MAGENTA = 5
CYAN = 6
BLANC = 7

# Touches Minitel
KEY_ENVOI = 1
KEY_RETOUR = 2
KEY_REPETITION = 3
KEY_GUIDE = 4
KEY_ANNULATION = 5
KEY_SOMMAIRE = 6
KEY_CORRECTION = 7
KEY_SUITE = 8
KEY_CNXFIN = 25

CONFIG_FILE = "/etc/minipi.json"

ser = serial.Serial(
    "/dev/serial0",
    baudrate=1200,
    bytesize=7,
    parity="E",
    stopbits=1,
    timeout=0.1,
)


def sys_run(cmd):
    """Executer une commande shell."""
    try:
        return subprocess.check_output(
            cmd,
            shell=True,
            stderr=subprocess.STDOUT,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        return e.output


def load_config():
    """Charger la configuration."""
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg):
    """Sauvegarder la configuration."""
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def get_hostname():
    return sys_run("hostname").strip()


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


def send(text: str):
    """Envoyer du texte."""
    text = _accents(text)
    ser.write(text.encode("latin-1", errors="ignore"))


def sendchr(code: int):
    """Envoyer un caractère."""
    ser.write(bytes([code]))


def sendesc(seq: str):
    """Envoyer ESC + séquence."""
    sendchr(27)
    send(seq)


def clear():
    """Effacer l'écran."""
    sendchr(12)


def clear_line(ligne):
    """Effacer une ligne complète rapidement."""
    print(ligne)
    pos(ligne, 1)
    send("\x1b[2K")


def pos(ligne: int, colonne: int = 1):
    """Position curseur."""

    if ligne == 1 and colonne == 1:
        sendchr(30)
    else:
        sendchr(31)
        sendchr(64 + ligne)
        sendchr(64 + colonne)


def cursor(visible: bool):
    """Afficher/cacher curseur."""
    sendchr(17 if visible else 20)


def color(c: int):
    """Couleur texte."""
    sendesc(chr(64 + c))


def textbg(ligne: int, colonne: int, text: str, bg: int, fg: int = BLANC):
    """Texte avec fond."""

    pos(ligne, colonne)

    sendesc(chr(80 + bg))
    sendesc(chr(64 + fg))

    send(" ")
    send(text.ljust(WIDTH - 1))

    sendesc(chr(64 + BLANC))
    sendesc(chr(80 + NOIR))


def header(title: str, bg: int = BLEU, fg: int = BLANC):
    """Header."""

    textbg(1, 1, title.center(WIDTH), bg, fg)


def fill(char: str, count: int):
    """Répéter caractère."""

    if count <= 0:
        return

    send(char)

    if count == 1:
        return

    remaining = count - 1

    while remaining > 0:
        n = min(remaining, 63)
        sendchr(18)
        sendchr(64 + n)
        remaining -= n


def disable_local_echo():
    """
    Désactiver l'echo local:
    ESC 0x3B (PRO3) + 0x60 (P_OFF) + 0x5A (MODEM_RX) + 0x51 (CLAVIER_TX)
    """
    ser.write(b"\x1b\x3b\x60\x5a\x51")
    time.sleep(0.1)


def read_event():
    """Lire un évènement."""

    c = ser.read(1)

    if not c:
        return None

    b = c[0]

    # Touches fonction
    if b == 0x13:
        k = ser.read(1)

        if not k:
            return None

        return ("KEY", k[0] - 64)

    # Caractère
    ch = c.decode("latin-1")

    if ch >= " ":
        return ("CHAR", ch)

    return None


def text_input(ligne, colonne, longueur, masked=False):
    """Champ texte."""

    data = ""

    pos(ligne, colonne)
    fill(".", longueur)

    pos(ligne, colonne)

    cursor(True)

    ser.reset_input_buffer()

    while True:

        ev = read_event()

        if not ev:
            continue

        et, val = ev

        if et == "CHAR":

            if len(data) < longueur:
                data += val
                send("*" if masked else val)

        elif et == "KEY":

            if val == KEY_CORRECTION and data:

                data = data[:-1]

                pos(ligne, colonne)
                fill(".", longueur)

                pos(ligne, colonne)

                send(("*" * len(data)) if masked else data)

                pos(ligne, colonne + len(data))

            elif val == KEY_ANNULATION:

                data = ""

                pos(ligne, colonne)
                fill(".", longueur)

                pos(ligne, colonne)

            elif val == KEY_ENVOI:

                cursor(False)

                if data.strip():
                    return data

            elif val == KEY_SOMMAIRE:

                cursor(False)
                return None


def clear_setup_area():
    """Effacer uniquement la zone centrale."""

    for l in range(2, 23):
        clear_line(l)


def draw_progress(active):
    """Dessiner progression."""

    steps = [
        "Wi-Fi",
        "Nom d'hôte",
        "Installation",
    ]

    pos(24, 2)

    for i, step in enumerate(steps):

        if i > 0:
            color(BLANC)
            send(" -> ")

        if i == active:
            color(BLANC)
        else:
            color(ROUGE)

        send(step)

    color(BLANC)


def setup_wifi():
    """Configuration Wi-Fi."""

    clear_setup_area()
    draw_progress(0)

    textbg(2, 1, "Configuration Wi-Fi".ljust(WIDTH), BLEU, BLANC)

    pos(5, 3)
    color(BLANC)
    send("SSID :")

    ssid = text_input(6, 3, 32)

    if not ssid:
        return False

    pos(9, 3)
    send("Mot de passe :")

    password = text_input(10, 3, 32, masked=True)

    pos(13, 3)
    color(CYAN)
    send("Connexion...")
    color(BLANC)

    ssid_escaped = ssid.replace('"', '\\"')
    password_escaped = password.replace('"', '\\"') if password else ""

    sys_run(f'nmcli con delete "{ssid_escaped}" 2>/dev/null')

    sys_run(
        f'nmcli con add type wifi ifname wlan0 con-name "{ssid_escaped}" ssid "{ssid_escaped}"'
    )

    if password:
        sys_run(f'nmcli con modify "{ssid_escaped}" wifi-sec.key-mgmt wpa-psk')

        sys_run(f'nmcli con modify "{ssid_escaped}" wifi-sec.psk "{password_escaped}"')

    result = sys_run(f'nmcli con up "{ssid_escaped}"')

    success = "successfully" in result.lower()

    pos(15, 3)

    if success:
        color(VERT)
        send("Connexion réussie !")
    else:
        color(ROUGE)
        send("Échec de la connexion")

    color(BLANC)

    time.sleep(2)

    return success


def setup_hostname():
    """Configuration hostname."""

    clear_setup_area()
    draw_progress(1)

    textbg(2, 1, "Nom d'hôte".ljust(WIDTH), JAUNE, NOIR)

    pos(5, 3)
    color(CYAN)
    send("Actuel : ")

    color(BLANC)
    send(get_hostname())

    pos(8, 3)
    color(BLANC)
    send("Nouveau nom :")

    new = text_input(10, 3, 30)

    if not new:
        return False

    new_host = new.strip()

    sys_run(f"hostnamectl set-hostname {new_host}")

    try:

        with open("/etc/hosts", "r", encoding="utf-8") as f:
            hosts = f.read()

        # Remplacer toute ligne 127.0.1.1
        hosts = re.sub(
            r"^127\.0\.1\.1\s+.*$",
            f"127.0.1.1\t{new_host}",
            hosts,
            flags=re.MULTILINE,
        )

        # Si aucune ligne n'existe
        if "127.0.1.1" not in hosts:
            hosts += f"\n127.0.1.1\t{new_host}\n"

        with open("/etc/hosts", "w", encoding="utf-8") as f:
            f.write(hosts)

    except Exception:
        pass

    cfg = load_config()
    cfg["HOSTNAME"] = new.strip()
    save_config(cfg)

    pos(13, 3)

    color(VERT)
    send("Nom mis à jour")

    color(BLANC)

    time.sleep(2)

    return True


def do_upgrade():
    """Mise à jour système."""

    clear_setup_area()
    draw_progress(2)

    textbg(2, 1, "Installation système".ljust(WIDTH), VERT, NOIR)

    output_line = 5

    def print_line(text):
        nonlocal output_line

        while len(text) > 0:

            if output_line > 22:
                output_line = 5

            clear_line(output_line)

            pos(output_line, 1)
            color(BLANC)

            send(text[:WIDTH])

            text = text[WIDTH:]

            output_line += 1

    # Télécharger config update
    try:

        with urllib.request.urlopen(
            "https://raw.githubusercontent.com/Mickmick21/MiniPi/refs/heads/main/latest.json",
            timeout=5,
        ) as r:

            cfg = json.loads(r.read().decode())

    except Exception as e:

        print_line("Erreur config :")
        print_line(str(e))

        time.sleep(5)

        return False

    # Mise à jour APT
    if cfg.get("doAptBeforeUpdate", False):

        cmds = [
            ("apt update", "Mise à jour des listes..."),
            ("apt upgrade -y", "Installation..."),
            ("apt autoremove -y", "Nettoyage..."),
        ]

        for cmd, label in cmds:

            textbg(3, 1, label.ljust(WIDTH), VERT, NOIR)

            with subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
            ) as proc:

                for raw_line in proc.stdout:

                    line = raw_line.decode(
                        "utf-8",
                        errors="replace",
                    ).rstrip()

                    if line:
                        print_line(line)

    # Télécharger et exécuter le script.
    script_url = cfg.get("updateScript")

    if script_url:

        textbg(3, 1, "Téléchargement du script...".ljust(WIDTH), VERT, NOIR)

        try:

            fd, tmp_path = tempfile.mkstemp(suffix=".py")

            try:

                os.close(fd)

                urllib.request.urlretrieve(script_url, tmp_path)

                textbg(
                    3,
                    1,
                    "Exécution du script...".ljust(WIDTH),
                    VERT,
                    NOIR,
                )

                with subprocess.Popen(
                    f"python3 {tmp_path}",
                    shell=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                ) as proc:

                    for raw_line in proc.stdout:

                        line = raw_line.decode(
                            "utf-8",
                            errors="replace",
                        ).rstrip()

                        if line:
                            print_line(line)

            finally:

                os.unlink(tmp_path)

        except Exception as e:

            print_line("Erreur :")
            print_line(str(e))

            return False

    else:

        print_line("Pas de script fourni.")

    textbg(3, 1, "Installation terminée".ljust(WIDTH), VERT, NOIR)

    return True


def main():

    clear()

    disable_local_echo()

    header("Installation", bg=VERT, fg=NOIR)

    cursor(False)

    setup_wifi()

    setup_hostname()

    do_upgrade()

    textbg(
        23,
        1,
        "Redémarrage automatique...".ljust(WIDTH),
        VERT,
        NOIR,
    )

    # Synchroniser disque
    sys_run("sync")

    time.sleep(3)

    # Reboot automatique
    sys_run("reboot")


if __name__ == "__main__":
    main()
