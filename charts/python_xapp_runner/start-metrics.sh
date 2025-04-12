#!/bin/bash

# Start xapp.py first
echo "Starting xapp.py..."
exec python3 -u /tmp/xApp/xapp.py &
XAPP_PID=$!

# Wait a moment to ensure xapp.py is fully started
sleep 2;

# Check if the environment variable to start perf is set
if [[ -n "$START_PERF" && "$START_PERF" == "true" ]]; then
    echo "Starting perf monitoring..."

    # Find the PID of the gnb process (replace with exact name if necessary)
    GNB_PID=$(ps aux | grep "[g]nb" | awk '{print $2}')

    if [ -z "$GNB_PID" ]; then
        echo "gnb process not found. Exiting.";
        exit 1;
    fi;

    # Start perf monitoring attached to the gnb PID in the background
    echo "Starting perf monitoring for gnb process (PID: $GNB_PID)..."
    perf record -e cycles,instructions,cache-misses -F 1000 -g -p $GNB_PID -o /mnt/data/perf.data &
    PERF_PID=$!
fi

# Wait until the gnb process ends, if it's being monitored
if [[ -n "$PERF_PID" ]]; then
    echo "Waiting for gnb process (PID: $GNB_PID) to finish..."
    wait $GNB_PID;

    # Wait for the perf process to finish recording
    echo "gnb process finished, stopping perf."
    wait $PERF_PID;
    
    # Send a graceful SIGTERM to xapp.py to stop it when gnb finishes
    echo "gnb process has finished. Sending SIGTERM to xapp.py (PID: $XAPP_PID)..."
    kill -TERM $XAPP_PID;
else
    # If no perf was run, just wait for xapp to finish
    echo "No perf monitoring. Waiting for xapp.py to finish..."
    wait $XAPP_PID;
fi

# End of script
echo "All processes have finished."
