#!/usr/bin/env python3
"""
Script de mise à jour MiniPi
"""

import urllib.request
import os
import shutil
import time

URL = "https://raw.githubusercontent.com/Mickmick21/MiniPi/refs/heads/main/minipi.py"
CHANGELOG_URL = "https://raw.githubusercontent.com/Mickmick21/MiniPi/refs/heads/main/new.txt"

TARGET = "/home/minitel/minipi.py"
TMP = "/tmp/minipi_new.py"


def fetch_changelog():
    """
    Télécharger les nouveautés.
    """
    try:
        with urllib.request.urlopen(CHANGELOG_URL, timeout=5) as r:
            return r.read().decode("utf-8", errors="replace")
    except OSError as e:
        return f"(Impossible de récupérer le changelog: {e})"


def main():
    """
    Fonction de Mise à Jour.
    """
    print("=== Mise à jour MiniPi ===\n")

    # Afficher les nouveautés
    changelog = fetch_changelog()
    print("Changelog:\n")
    print(changelog)
    print("\n-------------------------\n")

    print("Téléchargement...")
    urllib.request.urlretrieve(URL, TMP)

    print("Installation...")
    shutil.copy(TMP, TARGET)
    os.chmod(TARGET, 0o755)

    print("\nMise à jour terminée.")
    print("Merci de redémarrer")
    time.sleep(1)


if __name__ == "__main__":
    main()
