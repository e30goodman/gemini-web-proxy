#!/bin/sh
# gemini-login.sh — open the hidden browser (used by the proxy) on a VNC display
# so a human can log into Google. Run on the machine that hosts the proxy.
#
# The proxy drives a REAL Firefox via X11/AT-SPI on a headless virtual display
# (Xvfb). Firefox must be logged into Google there — but you cannot see that
# display. This script starts the same xvfb+firefox and shares the screen
# through VNC, so you can connect with any VNC client, sign in, and only then
# start the proxy.
#
# Usage:
#   ./gemini-login.sh              # default :2, VNC port 5902, wait for login
#   DISPLAY=:3 ./gemini-login.sh   # different virtual display
#   GEMINI_VNC_PORT=5903 ./gemini-login.sh
#
# What it does:
#   1. stop the DOM-watcher (so it does not tear the browser down mid-login)
#   2. start Xvfb + fluxbox (window manager) on the chosen display, if not up
#   3. start x11vnc sharing that display (install: apk add x11vnc)
#   4. open Firefox with the pump profile at https://accounts.google.com
#   5. print VNC connection info and WAIT until you press Enter after logging in
#   6. (optionally) restart the DOM-watcher
#
# Connect to VNC, pick "the Gemini account", sign in, go back to the shell and
# press Enter. Cookies are saved into the pump profile automatically.
set -u
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DISPLAY="${DISPLAY:-:2}"
VNC_PORT="${GEMINI_VNC_PORT:-$((5900 + ${DISPLAY#:}))}"
DISP_NUM="${DISPLAY#:}"
XVFB_LOCK="/tmp/.X${DISP_NUM}-lock"
X_SOCK="/tmp/.X11-unix/X${DISP_NUM}"
PROFILE="${GEMINI_PUMP_PROFILE:-$HOME/.mozilla/firefox/gemini-pump.default}"
FIREFOX="${GEMINI_FIREFOX_BIN:-/usr/bin/firefox}"
LOGIN_URL="${GEMINI_LOGIN_URL:-https://accounts.google.com}"

command -v Xvfb >/dev/null || { echo "need Xvfb (apk add xvfb xdotool)"; exit 1; }
command -v x11vnc >/dev/null || { echo "need x11vnc (apk add x11vnc)"; exit 1; }

echo "[login] display=$DISPLAY vnc=:$DISP_NUM (port $VNC_PORT) profile=$PROFILE"

# 1. stop the DOM-watcher so its idle-teardown won't kill the browser
if systemctl list-unit-files hermes-gemini-dom-watcher.service >/dev/null 2>&1 \
   && systemctl is-active --quiet hermes-gemini-dom-watcher.service; then
    echo "[login] stopping hermes-gemini-dom-watcher (telemetry teardown)"
    sudo -n systemctl stop hermes-gemini-dom-watcher.service 2>/dev/null \
        || systemctl --user stop hermes-gemini-dom-watcher.service 2>/dev/null || true
fi

# 2. make sure Xvfb + a window manager are up on this display
if [ ! -S "$X_SOCK" ]; then
    echo "[login] starting Xvfb on $DISPLAY"
    Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp >/tmp/gemini_login_xvfb.log 2>&1 &
    sleep 1
fi
export DISPLAY
if ! xdotool search --name "" >/dev/null 2>&1; then :; fi
pgrep -f "fluxbox -display $DISPLAY" >/dev/null || {
    echo "[login] starting fluxbox"
    fluxbox -display "$DISPLAY" >/tmp/gemini_login_fluxbox.log 2>&1 &
    sleep 1
}

# 3. share the display over VNC
if ! pgrep -f "x11vnc.*-display $DISPLAY" >/dev/null; then
    echo "[login] starting x11vnc on :$DISP_NUM (port $VNC_PORT)"
    x11vnc -display "$DISPLAY" -rfbport "$VNC_PORT" -shared -forever \
        -nopw >/tmp/gemini_login_vnc.log 2>&1 &
    sleep 2
fi

# 4. open Firefox at the Google login page
echo "[login] opening $LOGIN_URL in Firefox"
"$FIREFOX" --profile "$PROFILE" --no-remote "$LOGIN_URL" \
    >/tmp/gemini_login_ff.log 2>&1 &
sleep 3

cat <<'EOT'

=========================================================
 LOGIN WINDOW IS READY — CONNECT OVER VNC
=========================================================
 Connect a VNC client to this machine:

   host   : <this machine's IP>
   port   : $VNC_PORT   (display :$DISP_NUM)
   password: none

 (If you are remote, tunnel first: ssh -L $VNC_PORT:localhost:$VNC_PORT <host>)

 In the browser window that appears:
   1. go to https://accounts.google.com   (already open)
   2. sign in with the Google account that should serve Gemini
   3. you can close the VNC connection after the login form is accepted

 When you are signed in and see the Gemini home page — press Enter here.
 After that the proxy can read the cookies from the pump profile.
=========================================================
EOT
echo -n "[login] press Enter when you are logged in... "
read _x || true

# 5. cleanup
echo "[login] stopping x11vnc + Firefox, profile cookies saved"
pkill -f "x11vnc.*-display $DISPLAY" 2>/dev/null || true
sudo -n systemctl start hermes-gemini-dom-watcher.service 2>/dev/null \
    || systemctl --user start hermes-gemini-dom-watcher.service 2>/dev/null || true
echo "[login] done. Start the proxy (./run.sh or systemd) and log in is complete."