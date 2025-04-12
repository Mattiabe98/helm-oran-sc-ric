#!/bin/bash

# Start xapp.py first
echo "Starting xapp.py..."
exec python3 -u /tmp/xApp/xapp.py &
XAPP_PID=$!

# Wait a moment to ensure xapp.py is fully started
sleep 2;
sleep 10000;
# Check if the environment variable to start perf is set
if [[ -n "$START_PERF" && "$START_PERF" == "true" ]]; then
    echo "Starting perf monitoring..."

    # Find the PID of the gnb process (replace with exact name if necessary)
    GNB_PID=$(pgrep -f "gnb");

    if [ -z "$GNB_PID" ]; then
        echo "gnb process not found. Exiting.";
        sleep 10000;
        exit 1;
    fi;

    # Start perf monitoring attached to the gnb PID in the background
    echo "Starting perf monitoring for gnb process (PID: $GNB_PID)..."
    perf record -e cycles,instructions,cache-misses -F 1000 -g -p $GNB_PID -o /mnt/data/perf.data &
    PERF_PID=$!
fi

# Check if the environment variable to start turbostat is set
if [[ -n "$START_TURBOSTAT" && "$START_TURBOSTAT" == "true" ]]; then
    echo "Starting turbostat monitoring..."

    # Start turbostat in the background (write output to a file)
    turbostat --Summary --interval 1 -o /mnt/data/turbostat_output.txt &
    TURBOSTAT_PID=$!
fi

# Wait until the gnb process ends, if it's being monitored
if [[ -n "$PERF_PID" ]]; then
    echo "Waiting for gnb process (PID: $GNB_PID) to finish..."
    tail --pid=$GNB_PID -f /dev/null;

    # Wait for the perf process to finish recording
    echo "gnb process finished, stopping perf."
    tail --pid=$PERF_PID -f /dev/null;
fi

# Wait for the turbostat process to finish, if it's running
if [[ -n "$TURBOSTAT_PID" ]]; then
    echo "Stopping turbostat."
    tail --pid=$TURBOSTAT_PID -f /dev/null;
fi

# Send a graceful SIGTERM to xapp.py to stop it when gnb finishes
echo "gnb process has finished. Sending SIGTERM to xapp.py (PID: $XAPP_PID)..."
kill -TERM $XAPP_PID;

# End of script
echo "All processes have finished."
