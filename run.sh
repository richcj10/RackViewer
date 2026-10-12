#!/bin/sh
# Started by wayfire autostart; restarts RackView if it ever exits.
cd "$(dirname "$(readlink -f "$0")")"
while true; do
    [ -f rackview.log ] && [ $(stat -c %s rackview.log) -gt 1000000 ] && mv rackview.log rackview.log.old
    python3 rackview.py >> rackview.log 2>&1
    sleep 3
done
