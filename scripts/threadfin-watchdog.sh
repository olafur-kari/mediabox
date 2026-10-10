#!/bin/sh
# Kill orphaned ffmpeg processes inside Threadfin.
#
# Threadfin sometimes keeps its ffmpeg running after the viewer has gone. On a
# one-connection subscription that orphan holds the line, the provider then cuts
# the real viewer off every few seconds, and the player replays the last 5-10
# seconds on each reconnect. Only one upstream stream can work at a time anyway,
# so whenever there is more than one ffmpeg, keep the newest and kill the rest.
#
# Cron runs this once a minute; it checks four times, 15s apart.
#   * * * * * /home/oli/mediabox/scripts/threadfin-watchdog.sh >> /home/oli/mediabox/data/watchdog.log 2>&1

for _ in 1 2 3 4; do
  # "<pid> <seconds running>" for each ffmpeg, newest first
  procs=$(docker exec threadfin ps -eo pid=,etimes=,comm= 2>/dev/null \
    | awk '$3 == "ffmpeg" {print $1, $2}' | sort -k2 -n)
  if [ "$(printf '%s\n' "$procs" | grep -c .)" -gt 1 ]; then
    printf '%s\n' "$procs" | tail -n +2 | while read -r pid age; do
      echo "$(date '+%F %T') killing orphan ffmpeg pid $pid (running ${age}s)"
      docker exec threadfin kill "$pid"
    done
  fi
  sleep 15
done
