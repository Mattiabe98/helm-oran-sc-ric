#!/usr/bin/env python3
import socket
import json
import argparse
import signal
import subprocess
import datetime
import time
import threading
import queue
import logging
from contextlib import suppress

# Assuming lib.xAppBase is in the python path or current directory
from lib.xAppBase import xAppBase

# --- Turbostat Function ---
def get_turbostat_metrics():
    """Runs turbostat and parses its output."""
    try:
        # Run turbostat for 1 interval (implicitly 1 second if not specified otherwise by system defaults)
        # Adjust --interval if needed, but 1 second aligns with the goal.
        # Use --quiet to reduce header noise, but we still need to parse carefully.
        # Add options like --cpu to get per-core info if desired.
        result = subprocess.run(
            ["turbostat", "--Summary", "--quiet", "--interval", "1", "--num_iterations", "1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True
        )

        now = datetime.datetime.now(datetime.timezone.utc)
        lines = result.stdout.strip().split('\n')

        if len(lines) < 2:
            logging.warning("Turbostat output insufficient: %s", result.stdout)
            return None

        # Find the header and data lines (skip potential initial blank lines)
        header_line = None
        data_line = None
        for i, line in enumerate(lines):
            if line.strip() and header_line is None:
                header_line = line
            elif header_line is not None and line.strip():
                data_line = line
                break # Take the first data line after the header

        if not header_line or not data_line:
            logging.warning("Could not parse turbostat header/data lines. Output:\n%s", result.stdout)
            return None

        headers = header_line.split()
        values = data_line.split()

        if len(headers) != len(values):
            logging.warning("Turbostat header/value count mismatch. H: %s, V: %s", headers, values)
            return None

        metrics = dict(zip(headers, values))
        # Attempt to convert numeric values
        for key, value in metrics.items():
            try:
                metrics[key] = float(value)
            except ValueError:
                pass # Keep as string if conversion fails

        metrics['timestamp_utc'] = now.isoformat()
        return metrics

    except FileNotFoundError:
        logging.error("turbostat command not found. Please install it (e.g., linux-tools-common).")
        return None
    except subprocess.CalledProcessError as e:
        logging.error("Turbostat execution failed: %s\nStderr: %s", e, e.stderr)
        return None
    except Exception as e:
        logging.exception("Error getting turbostat metrics: %s", e)
        return None

# --- srsRAN JSON Metrics Parsing Logic (adapted) ---
def parse_srsran_metrics(metric_json):
    """Parses the srsRAN JSON and extracts relevant data."""
    parsed_data = {}
    timestamp = datetime.datetime.fromtimestamp(metric_json.get("timestamp", time.time()), datetime.timezone.utc)
    parsed_data['timestamp_utc'] = timestamp.isoformat()

    # --- UE List Metrics ---
    if "ue_list" in metric_json:
        parsed_data['ue_metrics'] = {}
        for ue_info in metric_json["ue_list"]:
            ue_container = ue_info.get("ue_container", {})
            rnti = ue_container.pop("rnti", None)
            pci = ue_container.pop("pci", None)
            if rnti is not None:
                rnti_hex = f"{rnti:x}"
                # Convert integers to floats for consistency
                for key, value in ue_container.items():
                   if isinstance(value, int):
                       ue_container[key] = float(value)
                parsed_data['ue_metrics'][rnti_hex] = {
                    "pci": pci,
                    "rnti_dec": rnti,
                    **ue_container # Unpack remaining fields
                }
        if not parsed_data['ue_metrics']:
            del parsed_data['ue_metrics'] # remove if empty

    # --- App Resource Usage ---
    if "app_resource_usage" in metric_json:
        usage_data = metric_json["app_resource_usage"]
        for key, value in usage_data.items():
            if isinstance(value, int):
                usage_data[key] = float(value)
        parsed_data['app_resource_usage'] = usage_data

    # --- RU/OFH Cell Metrics ---
    if "ru" in metric_json and "ofh_cells" in metric_json["ru"]:
        parsed_data['ofh_cells'] = {}
        for cell_cont in metric_json["ru"]["ofh_cells"]:
            cell = cell_cont.get("cell", {})
            pci = cell.get("pci")
            if pci is not None:
                cell_metrics = {}
                if cell.get("ul", {}).get("received_packets"):
                     # Convert integers to floats
                     ul_pkts = cell["ul"]["received_packets"]
                     for key, value in ul_pkts.items():
                        if isinstance(value, int):
                            ul_pkts[key] = float(value)
                     cell_metrics["ul_received_packets"] = ul_pkts
                # Add other RU metrics here if needed (DL, etc.)
                if cell_metrics:
                    parsed_data['ofh_cells'][pci] = cell_metrics
        if not parsed_data['ofh_cells']:
            del parsed_data['ofh_cells'] # remove if empty


    # Only return if we actually parsed something other than the timestamp
    return parsed_data if len(parsed_data) > 1 else None


# --- Main xApp Class ---
class CombinedXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port, srsran_udp_port):
        super(CombinedXapp, self).__init__(config, http_server_port, rmr_port)
        self._setup_logging()

        # Data storage (use thread-safe structures or locks if accessed by multiple threads directly)
        self.latest_e2_metrics = {} # Keyed by subscription_id, then potentially UE ID
        self.latest_srsran_data = None
        self.latest_turbostat_data = None
        self.data_lock = threading.Lock() # To protect access to latest_ data structures

        # srsRAN listener setup
        self.srsran_udp_port = srsran_udp_port
        self.srsran_queue = queue.Queue()
        self.srsran_listener_thread = None
        self.srsran_stop_event = threading.Event()

        # Turbostat poller setup
        self.turbostat_queue = queue.Queue()
        self.turbostat_poller_thread = None
        self.turbostat_stop_event = threading.Event()

        # Reporting Timer
        self.reporting_interval_sec = 1.0 # How often to print combined stats
        self.reporting_timer = None
        self.reporting_stop_event = threading.Event() # Use if timer runs in its own thread

    def _setup_logging(self):
        logging.basicConfig(level=logging.INFO,
                            format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
        # Quieten noisy libraries if needed
        # logging.getLogger("werkzeug").setLevel(logging.WARNING)


    # --- E2 Callback ---
    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id_assoc):
        # ue_id_assoc is the UE ID associated *at subscription time* for style 2
        timestamp_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
        logging.info("RIC Indication Received from %s (SubID: %s, Style: %s, UE Assoc: %s)",
                     e2_agent_id, subscription_id, kpm_report_style, ue_id_assoc)

        try:
            indication_hdr_dict = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data_dict = self.e2sm_kpm.extract_meas_data(indication_msg)
            logging.debug("E2 HDR Dict: %s", indication_hdr_dict)
            logging.debug("E2 MSG Dict: %s", meas_data_dict)


            e2_report = {
                'timestamp_utc': timestamp_utc,
                'agent_id': e2_agent_id,
                'kpm_report_style': kpm_report_style,
                'header': indication_hdr_dict,
                'measurements': {} # Store processed measurements here
            }

            granulPeriod = meas_data_dict.get("granulPeriod")
            if granulPeriod is not None:
                e2_report['granularity_ms'] = granulPeriod

            if kpm_report_style in [1, 2]:
                # Style 1: Cell level, Style 2: UE level (but msg format is similar for 'measData')
                if "measData" in meas_data_dict:
                    if kpm_report_style == 1:
                        e2_report['measurements']['cell'] = meas_data_dict["measData"]
                    elif kpm_report_style == 2 and ue_id_assoc is not None:
                         # Use the associated UE ID from the subscription
                         e2_report['measurements'][f'ue_{ue_id_assoc}'] = meas_data_dict["measData"]
                    else:
                         e2_report['measurements']['unknown_style2'] = meas_data_dict["measData"]

            elif kpm_report_style in [3, 4, 5]:
                # Styles 3, 4, 5 report per UE
                if "ueMeasData" in meas_data_dict:
                    for ue_id_report, ue_meas_data in meas_data_dict["ueMeasData"].items():
                        ue_key = f'ue_{ue_id_report}'
                        e2_report['measurements'][ue_key] = ue_meas_data.get("measData", {})
                        granulPeriodUe = ue_meas_data.get("granulPeriod")
                        if granulPeriodUe is not None:
                             e2_report['measurements'][ue_key]['granularity_ms'] = granulPeriodUe


            with self.data_lock:
                self.latest_e2_metrics[subscription_id] = e2_report
                # Optional: Limit the size if needed, e.g., keep only latest N reports

        except Exception as e:
            logging.exception("Error processing E2 indication (SubID: %s): %s", subscription_id, e)


    # --- srsRAN Listener Thread ---
    def _run_srsran_listener(self):
        logging.info(f"Starting srsRAN UDP listener on port {self.srsran_udp_port}")
        server_socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
        # Allow address reuse
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except AttributeError:
            pass # Some systems don't support SO_REUSEPORT
        try:
            server_socket.bind(("0.0.0.0", self.srsran_udp_port))
            server_socket.settimeout(1.0) # Timeout to allow checking stop event
        except OSError as e:
            logging.error(f"Failed to bind srsRAN UDP socket to port {self.srsran_udp_port}: {e}")
            return # Exit thread if bind fails

        buffer = ""
        max_buffer_size = 1024 * 64 # Increased buffer size for potentially large JSONs

        while not self.srsran_stop_event.is_set():
            try:
                data, addr = server_socket.recvfrom(max_buffer_size)
                if not data:
                    continue # Ignore empty packets

                buffer += data.decode('utf-8', errors='ignore')
                logging.debug(f"Received {len(data)} bytes from {addr}, buffer size: {len(buffer)}")

                # Process potential multiple JSON objects in the buffer
                while '}{' in buffer:
                    try:
                        # Find the boundary, assuming well-formed {...}{...}
                        split_index = buffer.find('}{') + 1
                        json_str = buffer[:split_index]
                        buffer = buffer[split_index:] # Keep the rest

                        metric_json = json.loads(json_str)
                        parsed = parse_srsran_metrics(metric_json)
                        if parsed:
                            self.srsran_queue.put(parsed) # Put parsed data onto the queue

                    except json.JSONDecodeError:
                        logging.warning(f"JSON decode error, problematic part: '{json_str[:100]}...' - resetting buffer segment.")
                        # Could attempt more robust recovery, but clearing part is safer
                        continue # Skip this malformed part
                    except Exception as e:
                         logging.exception("Error processing srsRAN JSON chunk: %s", e)

                # Try to process the remainder in the buffer if it looks like a complete JSON
                if buffer.strip().startswith('{') and buffer.strip().endswith('}'):
                    try:
                        metric_json = json.loads(buffer)
                        parsed = parse_srsran_metrics(metric_json)
                        if parsed:
                             self.srsran_queue.put(parsed)
                        buffer = "" # Clear buffer after successful parse
                    except json.JSONDecodeError:
                        # Incomplete JSON, wait for more data
                        logging.debug("Incomplete JSON in buffer, waiting for more data.")
                        if len(buffer) > max_buffer_size * 2: # Prevent runaway buffer growth
                           logging.warning("srsRAN buffer > max size, clearing.")
                           buffer = ""
                    except Exception as e:
                         logging.exception("Error processing final srsRAN JSON chunk: %s", e)
                         buffer = "" # Clear buffer on other errors

            except socket.timeout:
                continue # Check stop event again
            except OSError as e:
                # Handle potential socket errors during shutdown
                if self.srsran_stop_event.is_set():
                    logging.info("srsRAN listener socket error during shutdown.")
                else:
                    logging.error(f"srsRAN listener socket error: {e}")
                break # Exit loop on significant errors
            except Exception as e:
                logging.exception(f"Error in srsRAN listener loop: {e}")
                # Avoid continuous tight loop errors
                time.sleep(0.1)

        server_socket.close()
        logging.info("srsRAN UDP listener stopped.")

    # --- Turbostat Poller Thread ---
    def _run_turbostat_poller(self):
        logging.info("Starting Turbostat poller thread.")
        while not self.turbostat_stop_event.is_set():
            start_time = time.monotonic()
            metrics = get_turbostat_metrics()
            if metrics:
                self.turbostat_queue.put(metrics)

            # Wait until the next second boundary (approximately)
            elapsed = time.monotonic() - start_time
            wait_time = max(0, 1.0 - elapsed) # Ensure non-negative wait
            self.turbostat_stop_event.wait(timeout=wait_time)

        logging.info("Turbostat poller stopped.")

    # --- Periodic Reporting Function ---
    def _process_queues_and_report(self):
        """ Non-blocking check of queues and update latest data """
        # Process srsRAN queue
        while not self.srsran_queue.empty():
            try:
                srs_data = self.srsran_queue.get_nowait()
                with self.data_lock:
                    self.latest_srsran_data = srs_data
                self.srsran_queue.task_done()
            except queue.Empty:
                break
            except Exception as e:
                logging.exception("Error getting data from srsRAN queue: %s", e)

        # Process Turbostat queue
        while not self.turbostat_queue.empty():
            try:
                ts_data = self.turbostat_queue.get_nowait()
                with self.data_lock:
                    self.latest_turbostat_data = ts_data
                self.turbostat_queue.task_done()
            except queue.Empty:
                break
            except Exception as e:
                logging.exception("Error getting data from Turbostat queue: %s", e)


        # --- Generate Combined Output ---
        # Access data safely using the lock
        with self.data_lock:
            # Shallow copies to avoid holding lock during printing
            e2_metrics_copy = dict(self.latest_e2_metrics)
            srsran_data_copy = self.latest_srsran_data
            turbostat_data_copy = self.latest_turbostat_data

        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        report_lines = [f"\n--- Combined Report @ {now_str} ---"]

        # Turbostat Data
        if turbostat_data_copy:
            ts_time = turbostat_data_copy.get('timestamp_utc', 'N/A')
            pkg_watts = turbostat_data_copy.get('PkgWatt', 'N/A')
            cor_watts = turbostat_data_copy.get('CorWatt', 'N/A')
            gfx_watts = turbostat_data_copy.get('GFXWatt', 'N/A') # May not be present
            report_lines.append(f"  Turbostat ({ts_time}): PkgWatt={pkg_watts}, CorWatt={cor_watts}, GFXWatt={gfx_watts}")
            # Add more turbostat fields as needed, e.g., Busy%, Bzy_MHz
        else:
            report_lines.append("  Turbostat: No data available")

        # srsRAN Data
        if srsran_data_copy:
            srs_time = srsran_data_copy.get('timestamp_utc', 'N/A')
            report_lines.append(f"  srsRAN ({srs_time}):")
            if 'ue_metrics' in srsran_data_copy:
                 for rnti, ue_data in srsran_data_copy['ue_metrics'].items():
                      # Extract key metrics like DL/UL MCS, CQI, throughput etc.
                      phr = ue_data.get('mac_stats',{}).get('phr')
                      cqi = ue_data.get('mac_stats',{}).get('cqi')
                      dl_bler = ue_data.get('dl_bler',{}).get('bler')
                      ul_bler = ue_data.get('ul_bler',{}).get('bler')
                      report_lines.append(f"    UE {rnti}: PHR={phr}, CQI={cqi}, DL_BLER={dl_bler:.2f}, UL_BLER={ul_bler:.2f}") # Format float
            if 'app_resource_usage' in srsran_data_copy:
                cpu = srsran_data_copy['app_resource_usage'].get('cpu_percent_total')
                mem = srsran_data_copy['app_resource_usage'].get('mem_percent')
                report_lines.append(f"    AppUsage: CPU={cpu:.1f}%, MEM={mem:.1f}%")
            # Add OFH cell stats if needed
        else:
            report_lines.append("  srsRAN: No data available")

        # E2 Metrics Data (Iterate through active subscriptions)
        if e2_metrics_copy:
            report_lines.append(f"  E2 Metrics:")
            for sub_id, e2_data in e2_metrics_copy.items():
                e2_time = e2_data.get('timestamp_utc', 'N/A')
                agent = e2_data.get('agent_id','N/A')
                style = e2_data.get('kpm_report_style','N/A')
                report_lines.append(f"    SubID {sub_id} (Agent: {agent}, Style: {style}, Time: {e2_time}):")
                meas = e2_data.get('measurements', {})
                if not meas:
                    report_lines.append("      No measurements received.")
                for key, values in meas.items(): # key is 'cell', 'ue_X', etc.
                     # Format specific metrics nicely
                     dl_thr = values.get('DRB.UEThpDl')
                     ul_thr = values.get('DRB.UEThpUl')
                     dl_prb = values.get('DRB.PRBUsedDl')
                     ul_prb = values.get('DRB.PRBUsedUl')
                     meas_str = f"DL_Thp={dl_thr}, UL_Thp={ul_thr}, DL_PRB={dl_prb}, UL_PRB={ul_prb}"
                     # Add other common metrics you subscribe to
                     report_lines.append(f"      {key}: {meas_str}") # Print key metrics

        else:
            report_lines.append("  E2 Metrics: No active subscriptions or data received")

        print('\n'.join(report_lines))

        # Reschedule the timer for the next interval (important!)
        if not self.reporting_stop_event.is_set():
             self.reporting_timer = threading.Timer(self.reporting_interval_sec, self._process_queues_and_report)
             self.reporting_timer.start()


    # --- Start Function (Marked for xAppBase) ---
    @xAppBase.start_function
    def start(self, e2_node_id, kpm_report_style, ue_ids, metric_names):
        logging.info("Starting Combined xApp...")

        # Start srsRAN listener thread
        self.srsran_stop_event.clear()
        self.srsran_listener_thread = threading.Thread(target=self._run_srsran_listener, daemon=True)
        self.srsran_listener_thread.start()

        # Start Turbostat poller thread
        self.turbostat_stop_event.clear()
        self.turbostat_poller_thread = threading.Thread(target=self._run_turbostat_poller, daemon=True)
        self.turbostat_poller_thread.start()

        # Start the periodic reporting
        logging.info(f"Starting periodic reporting every {self.reporting_interval_sec} seconds.")
        self.reporting_stop_event.clear()
        # Initial immediate call might be useful, then schedule
        # self._process_queues_and_report() # Optional immediate report
        self.reporting_timer = threading.Timer(self.reporting_interval_sec, self._process_queues_and_report)
        self.reporting_timer.start()


        # --- E2 Subscription Logic (existing logic) ---
        report_period_ms = 1000 # E2 report period in milliseconds
        granul_period_ms = 1000 # E2 granularity period

        # Use functools.partial for cleaner callback argument binding if preferred
        # Default callback without UE ID association
        subscription_callback = lambda agent, sub, hdr, msg: self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, None)

        if kpm_report_style == 1:
            logging.info("Subscribing E2 Style 1: Node=%s, Metrics=%s", e2_node_id, metric_names)
            self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period_ms, metric_names, granul_period_ms, subscription_callback)

        elif kpm_report_style == 2:
            if not ue_ids:
                logging.error("UE ID is required for KPM Report Style 2 subscription.")
                self.stop() # Stop if required info is missing
                return
            target_ue_id = ue_ids[0]
            # Bind the specific UE ID for this subscription to the callback
            subscription_callback = lambda agent, sub, hdr, msg: self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, target_ue_id)
            logging.info("Subscribing E2 Style 2: Node=%s, UE=%s, Metrics=%s", e2_node_id, target_ue_id, metric_names)
            self.e2sm_kpm.subscribe_report_service_style_2(e2_node_id, report_period_ms, target_ue_id, metric_names, granul_period_ms, subscription_callback)

        elif kpm_report_style == 3:
             if (len(metric_names) > 1):
                 metric_names = metric_names[0] # Keep only first for style 3
                 logging.warning("Style 3 supports 1 metric. Using: %s", metric_names)
             # Dummy condition - replace with actual conditions if needed
             matchingConds = [{'matchingCondChoice': ('testCondInfo', {'testType': ('ul-rSRP', 'true'), 'testExpr': 'is-present'})}] # Example: just check if ul-rSRP exists
             logging.info("Subscribing E2 Style 3: Node=%s, Metrics=%s", e2_node_id, metric_names)
             self.e2sm_kpm.subscribe_report_service_style_3(e2_node_id, report_period_ms, matchingConds, metric_names, granul_period_ms, subscription_callback)

        # Add Styles 4 and 5 similarly if needed, ensuring correct callback association

        else:
            logging.error("Subscription for E2SM_KPM Report Service Style %s is not supported", kpm_report_style)
            self.stop() # Stop if style is not supported
            # exit(1) # Avoid hard exit in library code

    # --- Stop Function (Override base class) ---
    def stop(self):
        logging.info("Stopping Combined xApp...")

        # 1. Stop E2 Subscriptions (calls base class unsubscribe)
        super(CombinedXapp, self).stop() # Important to call parent stop

        # 2. Stop Reporting Timer
        if self.reporting_timer:
            self.reporting_stop_event.set() # Signal the timer loop (if it were a thread)
            self.reporting_timer.cancel() # Stop the pending timer
            logging.info("Reporting timer cancelled.")

        # 3. Stop Turbostat Poller Thread
        if self.turbostat_poller_thread and self.turbostat_poller_thread.is_alive():
            logging.info("Signalling Turbostat poller to stop...")
            self.turbostat_stop_event.set()
            self.turbostat_poller_thread.join(timeout=2.0) # Wait for thread exit
            if self.turbostat_poller_thread.is_alive():
                logging.warning("Turbostat poller thread did not exit cleanly.")

        # 4. Stop srsRAN Listener Thread
        if self.srsran_listener_thread and self.srsran_listener_thread.is_alive():
            logging.info("Signalling srsRAN listener to stop...")
            self.srsran_stop_event.set()
            # Send a dummy packet to unblock the recvfrom call if needed
            with suppress(Exception): # Avoid errors if socket already closed
                 sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                 sock.sendto(b'', ('127.0.0.1', self.srsran_udp_port))
                 sock.close()
            self.srsran_listener_thread.join(timeout=2.0) # Wait for thread exit
            if self.srsran_listener_thread.is_alive():
                logging.warning("srsRAN listener thread did not exit cleanly.")

        # Clean up queues potentially? (Usually not needed with daemon threads)
        logging.info("Combined xApp stopped.")

    # Override signal handler to call our custom stop
    def signal_handler(self, signum, frame):
        self.stop()


# --- Main Execution Block ---
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Combined xApp Example')
    # xAppBase args
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")
    # E2 Subscription args
    parser.add_argument("--e2_node_id", type=str, default='gnbd_001_001_00019b_1', help="Target E2 Node ID (adjust to your setup)") # Example srsRAN gNB ID format
    parser.add_argument("--ran_func_id", type=int, default=2, help="E2SM-KPM RAN function ID (usually 2)")
    parser.add_argument("--kpm_report_style", type=int, default=1, help="KPM Report Style (1-5)")
    parser.add_argument("--ue_ids", type=str, default='0', help="Comma-separated UE IDs (required for Style 2, used by Style 5)")
    parser.add_argument("--metrics", type=str, default='DRB.UEThpDl,DRB.UEThpUl', help="Comma-separated E2 Metrics")
    # srsRAN listener arg
    parser.add_argument("--srsran_udp_port", type=int, default=55555, help="UDP Port to listen for srsRAN metrics")
    # Turbostat arg (optional, interval is fixed in code for now)
    # parser.add_argument("--turbostat_interval", type=float, default=1.0, help="Turbostat polling interval (seconds)") # Could make interval configurable

    args = parser.parse_args()

    # Basic logging setup for the main script part
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - MAIN - %(message)s')

    # Process args
    config = args.config
    e2_node_id = args.e2_node_id
    ran_func_id = args.ran_func_id
    kpm_report_style = args.kpm_report_style
    metrics = args.metrics.split(",") if args.metrics else []
    ue_ids = []
    if args.ue_ids:
        try:
            ue_ids = list(map(int, args.ue_ids.split(",")))
        except ValueError:
            logging.error("Invalid UE IDs format. Should be comma-separated integers.")
            exit(1)

    if kpm_report_style == 2 and not ue_ids:
        logging.error("KPM Report Style 2 requires at least one UE ID via --ue_ids.")
        exit(1)
    if kpm_report_style == 5 and len(ue_ids) < 2:
         logging.error("KPM Report Style 5 requires at least two UE IDs via --ue_ids.")
         # You could add dummy IDs here like the original code, but erroring is safer
         exit(1)


    # Create the CombinedXapp instance
    myXapp = CombinedXapp(config, args.http_server_port, args.rmr_port, args.srsran_udp_port)
    myXapp.e2sm_kpm.set_ran_func_id(ran_func_id) # Set RAN Function ID for KPM service model

    # Connect Unix signals to the xApp's signal handler for graceful shutdown
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler) # Catches Ctrl+C

    try:
        # Start the xApp's main logic and E2 subscription
        # The start method now also launches the listener/poller threads and reporting timer.
        myXapp.start(e2_node_id, kpm_report_style, ue_ids, metrics)

        # Keep the main thread alive while background threads run
        # The xAppBase likely has its own internal loop or waits,
        # but if not, we might need a wait here.
        # For now, assume xAppBase handles the main loop blocking.
        # If the script exits immediately, add a wait loop here:
        # while True:
        #     try:
        #         time.sleep(1)
        #     except KeyboardInterrupt: # Let signal handler catch Ctrl+C
        #         break

    except Exception as main_err:
        logging.exception("Unhandled exception in main execution: %s", main_err)
    finally:
        # Ensure stop is called even if start fails or main loop has issues
        logging.info("Main execution finished or interrupted, ensuring xApp stops.")
        myXapp.stop()
