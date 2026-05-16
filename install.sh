#!/bin/bash
# install.sh

set -e

VERT="\033[1;32m"
JAUNE="\033[1;33m"
ROUGE="\033[1;31m"
CYAN="\033[1;36m"
RESET="\033[0m"

ok()   { echo -e "${VERT}  [OK]${RESET}  $1"; }
info() { echo -e "${CYAN}  [..]${RESET}  $1"; }
warn() { echo -e "${JAUNE}  [!]${RESET}   $1"; }
err()  { echo -e "${ROUGE}  [ERR]${RESET} $1"; exit 1; }


[ "$(id -u)" -eq 0 ] || err "Ce script doit être exécuté en tant que root (sudo bash install.sh)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║       Installation de MiniPi         ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════╝${RESET}"
echo ""

CONFIG_TXT="/boot/firmware/config.txt"
CMDLINE_TXT="/boot/firmware/cmdline.txt"
MINIPI_DIR="/home/minitel"
MINIPI_USER="minitel"
MINIPI_SCRIPT="$MINIPI_DIR/minipi.py"
SERVICE_FILE="/etc/systemd/system/minipi.service"

info "Mise à jour des listes de paquets..."
apt-get update -qq

info "Installation des dépendances..."
apt-get install -y -qq \
    python3-serial \
    python3-websocket \
    || err "Échec de l'installation des dépendances APT"

ok "Dépendances installées"

info "Désactivation des services inutiles..."

for svc in bluetooth avahi-daemon udisks2 ModemManager; do
    if systemctl is-enabled "$svc" &>/dev/null; then
        systemctl disable --now "$svc" &>/dev/null && ok "  $svc désactivé" || warn "  $svc : échec désactivation"
    else
        info "  $svc : déjà désactivé ou absent"
    fi
done

info "Configuration du port série (ttyAMA0)..."

if ! grep -q "^dtoverlay=disable-bt" "$CONFIG_TXT"; then
    echo "" >> "$CONFIG_TXT"
    echo "# MiniPi : libérer ttyAMA0 du Bluetooth" >> "$CONFIG_TXT"
    echo "dtoverlay=disable-bt" >> "$CONFIG_TXT"
    ok "Overlay disable-bt ajouté"
else
    info "Overlay disable-bt déjà présent"
fi

if ! grep -q "^enable_uart=1" "$CONFIG_TXT"; then
    echo "enable_uart=1" >> "$CONFIG_TXT"
    ok "enable_uart=1 ajouté"
else
    info "enable_uart=1 déjà présent"
fi

if grep -q "console=serial0" "$CMDLINE_TXT" || grep -q "console=ttyAMA0" "$CMDLINE_TXT"; then
    sed -i 's/console=serial0,[0-9]* //g' "$CMDLINE_TXT"
    sed -i 's/console=ttyAMA0,[0-9]* //g' "$CMDLINE_TXT"
    ok "Console série retirée de cmdline.txt"
else
    info "Pas de console série dans cmdline.txt"
fi

if systemctl is-enabled hciuart &>/dev/null; then
    systemctl disable --now hciuart &>/dev/null && ok "hciuart désactivé" || warn "hciuart : échec"
else
    info "hciuart : déjà désactivé ou absent"
fi


info "Activation de SSH..."
systemctl enable --now ssh &>/dev/null && ok "SSH activé" || warn "SSH : déjà actif ou erreur"

info "Préparation du répertoire MiniPi..."

if ! id "$MINIPI_USER" &>/dev/null; then
    useradd -m -s /bin/bash "$MINIPI_USER"
    ok "Utilisateur '$MINIPI_USER' créé"
else
    info "Utilisateur '$MINIPI_USER' déjà existant"
fi

usermod -aG dialout "$MINIPI_USER"
ok "Utilisateur ajouté au groupe dialout"

mkdir -p "$MINIPI_DIR"

info "Téléchargement de minipi.py..."

MINIPI_URL="https://raw.githubusercontent.com/Mickmick21/MiniPi/refs/heads/main/minipi_setup.py"

if curl -fsSL "$MINIPI_URL" -o "$MINIPI_SCRIPT" 2>/dev/null; then
    chown "$MINIPI_USER:$MINIPI_USER" "$MINIPI_SCRIPT"
    chmod 755 "$MINIPI_SCRIPT"
    ok "minipi.py téléchargé"
else
    warn "Téléchargement échoué - vous devrez copier minipi.py manuellement dans $MINIPI_DIR"
    touch "$MINIPI_SCRIPT"
    chown "$MINIPI_USER:$MINIPI_USER" "$MINIPI_SCRIPT"
fi

info "Création du service systemd minipi..."

cat > "$SERVICE_FILE" << 'EOF'
[Unit]
Description=MiniPi Minitel Interface
# Démarrer le plus tôt possible, sans dépendances réseau
DefaultDependencies=no
After=dev-ttyAMA0.device
Wants=dev-ttyAMA0.device

[Service]
Type=simple
User=root
ExecStart=/usr/bin/python3 /home/minitel/minipi.py
Restart=always
RestartSec=2
# Donner accès au port série
SupplementaryGroups=dialout

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable minipi.service
ok "Service minipi créé et activé"

echo ""
echo -e "${VERT}╔══════════════════════════════════════╗${RESET}"
echo -e "${VERT}║     Installation terminée !          ║${RESET}"
echo -e "${VERT}╚══════════════════════════════════════╝${RESET}"
echo ""
echo -e "  ${CYAN}Prochaines étapes :${RESET}"
echo -e "  1. Branchez le Minitel"
echo ""
echo -e "  2. Redémarrez le Pi :"
echo -e "     ${JAUNE}sudo reboot${RESET}"
echo ""
echo -e "  Au prochain démarrage, MiniPi se lancera automatiquement."
echo ""
