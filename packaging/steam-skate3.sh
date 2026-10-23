#!/usr/bin/env bash
# Steam launcher for Skate 3 Recompiled+ on Linux and the Steam Deck.
# From mchughalex/skate3recomp#191 by Drewsipher.
#
# Add this file to Steam as a non-Steam game. It works in Game Mode and
# Desktop Mode. Extra arguments are passed to the game, for example
# "%command% --fullscreen=false" in Steam's launch options.
set -uo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
GAME="$DIR/skate3"
if [ ! -x "$GAME" ]; then
  echo "steam-skate3: no skate3 executable next to this script ($DIR)" >&2
  exit 1
fi

# Steam starts non-Steam games with its own runtime's LD_LIBRARY_PATH. The game
# uses the system's GTK, Vulkan and udev and finds its runtime library next to
# itself, so use the system libraries only.
unset LD_LIBRARY_PATH

# One copy at a time. A copy left running (for example across a sleep/resume
# where Steam lost track of it) shares the controller and settings with a new
# one: both react to every button press and they fight over the screen.
# SIGKILL because the game ignores SIGTERM.
for pid in $(pgrep -x skate3); do
  if [ "$(readlink -f /proc/"$pid"/exe 2>/dev/null)" = "$GAME" ]; then
    echo "steam-skate3: stopping a copy of the game that was left running (pid $pid)" >&2
    kill -9 "$pid" 2>/dev/null
  fi
done
for _ in $(seq 1 30); do
  pgrep -x skate3 > /dev/null || break
  sleep 0.1
done

cd "$DIR" || exit 1
# Desktop Mode suspends on idle even while a game is running, and the game's
# GPU context does not survive the resume. Block idle sleep while the game
# runs; harmless in Game Mode.
if systemd-inhibit --what=idle --mode=block --why=probe true 2> /dev/null; then
  exec systemd-inhibit --what=idle --mode=block --why="Skate 3 is running" "$GAME" "$@"
fi
exec "$GAME" "$@"
