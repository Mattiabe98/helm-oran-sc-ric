#!/usr/bin/env python3
import sys
import argparse
import signal
from lib.xAppBase import xAppBase # Assuming this is your xApp base library
from datetime import datetime, timedelta
import re # For parsing CPU ranges
import threading # For locking and timer
import time # For sleep in main if needed, and time functions

# Helper function to parse CPU allocation strings
def parse_cpu_allocation(cpu_str):
    """
    Parses a CPU allocation string like "0-2,5,7-8" into a sorted list of unique integers.
    Returns None if any part of the string is invalid.
    """
    if not cpu_str:
        return []
    cpus = set()
    parts = cpu_str.split(',')
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if '-' in part:
            try:
                # Ensure there's only one hyphen for a simple range a-b
                if part.count('-') > 1:
                    print(f"Error: Invalid CPU range format (multiple hyphens) '{part}'.")
                    return None
                start_str, end_str = part.split('-', 1)
                start = int(start_str)
                end = int(end_str)
                if start > end:
                    print(f"Error: Invalid CPU range '{part}', start > end.")
                    return None
                cpus.update(range(start, end + 1))
            except ValueError:
                print(f"Error: Invalid CPU range format '{part}'. Could not parse numbers.")
                return None
        else:
            try:
                cpus.add(int(part))
            except ValueError:
                print(f"Error: Invalid CPU number format '{part}'.")
                return None
    return sorted(list(cpus))

class MyXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port, tdp_min_watts, tdp_max_watts,
                 aggregation_check_interval_sec=1.0, aggregation_grace_period_ms=500):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)

        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = f"/mnt/data/xapp/xapp_{self.timestamp}.txt"
        print(f"Logging RIC Indication data to: {self.log_file}")

        self.tdp_min_watts = tdp_min_watts
        self.tdp_max_watts = tdp_max_watts
        print(f"Desired TDP Limit Range: {self.tdp_min_watts}W - {self.tdp_max_watts}W")
        
        self.e2_node_custom_configs = {} # To store QoS/CPU info keyed by e2_node_id

        # For KPM aggregation
        self.kpm_report_buffers = {}
        self.expected_du_ids = set()
        self.aggregation_lock = threading.Lock()
        self.aggregation_check_interval_sec = aggregation_check_interval_sec
        self.aggregation_grace_period_ms = aggregation_grace_period_ms
        self.metric_name_dl_thp = "DRB.UEThpDl" # Standardized metric name for DL throughput
        self.metric_name_ul_thp = "DRB.UEThpUl" # Standardized metric name for UL throughput
        
        self._stop_event = threading.Event() # To signal the timer thread to stop
        self.processing_timer = None
        self._start_periodic_processor() # Start the aggregation timer

    def _start_periodic_processor(self):
        """Starts or restarts the periodic aggregation processor timer."""
        if not self._stop_event.is_set(): # Only schedule if not stopping
            self.processing_timer = threading.Timer(self.aggregation_check_interval_sec, self._periodic_aggregation_processor)
            self.processing_timer.daemon = True # Allows main program to exit even if timer is alive
            self.processing_timer.start()

    def _periodic_aggregation_processor(self):
        """
        Periodically checks buffered KPM reports and aggregates them if complete
        or if a grace period has expired.
        """
        # print(f"[{datetime.now()}] Periodic aggregator running...") # Debug print
        with self.aggregation_lock:
            current_time = datetime.now()
            # Iterate over a copy of keys because we might modify the dict during iteration
            for collet_start_time_str in list(self.kpm_report_buffers.keys()):
                buffer_entry = self.kpm_report_buffers.get(collet_start_time_str)
                if not buffer_entry: # Should not happen if key was from list(keys())
                    continue

                try:
                    # Assuming ColletStartTime format is "YYYY-MM-DD HH:MM:SS"
                    collet_start_dt = datetime.strptime(collet_start_time_str, "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    print(f"Error parsing ColletStartTime: {collet_start_time_str} in buffer. Removing entry.")
                    del self.kpm_report_buffers[collet_start_time_str]
                    continue
                
                granul_period_ms = buffer_entry.get("granul_period_ms", 1000) # Use stored or default
                
                all_reported = len(buffer_entry["received_from_dus"]) == len(self.expected_du_ids)
                
                interval_end_time = collet_start_dt + timedelta(milliseconds=granul_period_ms)
                grace_period_expiry_time = interval_end_time + timedelta(milliseconds=self.aggregation_grace_period_ms)
                grace_period_expired = current_time > grace_period_expiry_time

                if all_reported or grace_period_expired:
                    log_agg_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                    agg_header = f"\n--- Aggregated KPM Data for Interval Starting: {collet_start_time_str} (Aggregated at: {log_agg_time}) ---"
                    print(agg_header)
                    print(f"  Total {self.metric_name_dl_thp}: {buffer_entry['total_dl_thp']:.2f}")
                    print(f"  Total {self.metric_name_ul_thp}: {buffer_entry['total_ul_thp']:.2f}")
                    print(f"  Reported DUs ({len(buffer_entry['received_from_dus'])}/{len(self.expected_du_ids)}): {sorted(list(buffer_entry['received_from_dus']))}")
                    
                    missing_dus_info = ""
                    if grace_period_expired and not all_reported:
                        missing_dus = self.expected_du_ids - buffer_entry["received_from_dus"]
                        missing_dus_info = f"  Grace period expired. Missing reports from: {sorted(list(missing_dus))}"
                        print(missing_dus_info)
                    print("-----------------------------------------------------------------------------------------------------")
                    
                    with open(self.log_file, "a", buffering=1) as file_obj:
                        file_obj.write(agg_header + "\n")
                        file_obj.write(f"  Total {self.metric_name_dl_thp}: {buffer_entry['total_dl_thp']:.2f}\n")
                        file_obj.write(f"  Total {self.metric_name_ul_thp}: {buffer_entry['total_ul_thp']:.2f}\n")
                        file_obj.write(f"  Reported DUs ({len(buffer_entry['received_from_dus'])}/{len(self.expected_du_ids)}): {sorted(list(buffer_entry['received_from_dus']))}\n")
                        if missing_dus_info:
                             file_obj.write(missing_dus_info + "\n")
                        file_obj.write("-----------------------------------------------------------------------------------------------------\n")
                        file_obj.flush()

                    del self.kpm_report_buffers[collet_start_time_str] # Remove processed entry
        
        self._start_periodic_processor() # Reschedule the timer

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id_for_style2):
        if kpm_report_style == 2:
            print(f"\nRIC Indication Received from {e2_agent_id} for Subscription ID: {subscription_id}, KPM Report Style: {kpm_report_style}, UE ID: {ue_id_for_style2}")
        else:
            print(f"\nRIC Indication Received from {e2_agent_id} for Subscription ID: {subscription_id}, KPM Report Style: {kpm_report_style}")

        try:
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            print(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}")
            with open(self.log_file, "a", buffering=1) as file_obj:
                log_time = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
                file_obj.write(f"[{log_time}] ERROR processing indication from {e2_agent_id} for sub {subscription_id}: {e}\n")
                file_obj.write(f"Header: {indication_hdr}\n")
                file_obj.write(f"Message: {indication_msg}\n")
            return

        # Log individual report details
        def redirect_output_to_file(output, file_obj):
            file_obj.write(output + "\n")
            file_obj.flush()

        with open(self.log_file, "a", buffering=1) as file_obj:
            timestamp_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            log_prefix = f"[{timestamp_log}] [DU: {e2_agent_id}] [SubID: {subscription_id}]"

            redirect_output_to_file(f"{log_prefix} E2SM_KPM RIC Indication Content:", file_obj)
            # print(f"{log_prefix} E2SM_KPM RIC Indication Content:") # Already printed above

            collet_start_time = indication_hdr_extracted.get('colletStartTime', 'N/A')
            redirect_output_to_file(f"{log_prefix} -ColletStartTime: {collet_start_time}", file_obj)
            # print(f"-ColletStartTime: {collet_start_time}")

            redirect_output_to_file(f"{log_prefix} -Measurements Data:", file_obj)
            # print("-Measurements Data:")

            granulPeriod_val = meas_data.get("granulPeriod", None)
            if granulPeriod_val is not None:
                redirect_output_to_file(f"{log_prefix} -granulPeriod: {granulPeriod_val}", file_obj)
                # print(f"-granulPeriod: {granulPeriod_val}")

            if kpm_report_style in [1, 2]:
                if "measData" in meas_data:
                    for metric_name, value in meas_data["measData"].items():
                        redirect_output_to_file(f"{log_prefix} --Metric: {metric_name}, Value: {value}", file_obj)
                        # print(f"--Metric: {metric_name}, Value: {value}")
                else:
                    redirect_output_to_file(f"{log_prefix} --No 'measData' found in KPM Style {kpm_report_style} report.", file_obj)
            # Add comprehensive logging for styles 3,4,5 if needed, similar to style 1/2
            # ...

        # --- Aggregation Logic ---
        collet_start_time_str = indication_hdr_extracted.get('colletStartTime')
        if not collet_start_time_str:
            print(f"Warning: ColletStartTime not found in KPM header from {e2_agent_id}. Cannot aggregate.")
            return

        current_dl_thp = 0.0
        current_ul_thp = 0.0
        # granulPeriod_val already extracted above for logging

        if kpm_report_style in [1, 2]: # Cell-level reports
            if "measData" in meas_data:
                dl_val_list = meas_data["measData"].get(self.metric_name_dl_thp)
                ul_val_list = meas_data["measData"].get(self.metric_name_ul_thp)
                if dl_val_list and isinstance(dl_val_list, list) and len(dl_val_list) > 0:
                    try: current_dl_thp = float(dl_val_list[0])
                    except (ValueError, TypeError): print(f"Warning: Could not parse DL Thp value {dl_val_list[0]} from {e2_agent_id}")
                if ul_val_list and isinstance(ul_val_list, list) and len(ul_val_list) > 0:
                    try: current_ul_thp = float(ul_val_list[0])
                    except (ValueError, TypeError): print(f"Warning: Could not parse UL Thp value {ul_val_list[0]} from {e2_agent_id}")
        # Add logic for UE-level reports (styles 3, 4, 5) if aggregation from them is needed.
        # This would involve iterating meas_data["ueMeasData"] and summing up.

        with self.aggregation_lock:
            if collet_start_time_str not in self.kpm_report_buffers:
                self.kpm_report_buffers[collet_start_time_str] = {
                    "total_dl_thp": 0.0,
                    "total_ul_thp": 0.0,
                    "received_from_dus": set(),
                    "granul_period_ms": granulPeriod_val if granulPeriod_val is not None else 1000,
                    "first_report_arrival_time": datetime.now() # For debugging or more advanced timeout logic
                }
            
            buffer_entry = self.kpm_report_buffers[collet_start_time_str]
            
            if e2_agent_id not in buffer_entry["received_from_dus"]: # Add only once per DU per interval
                buffer_entry["total_dl_thp"] += current_dl_thp
                buffer_entry["total_ul_thp"] += current_ul_thp
                buffer_entry["received_from_dus"].add(e2_agent_id)
                if granulPeriod_val is not None and buffer_entry["granul_period_ms"] == 1000: # Update if default was used
                     buffer_entry["granul_period_ms"] = granulPeriod_val
            # else:
                # print(f"Note: DU {e2_agent_id} may have re-sent data for interval {collet_start_time_str}. Using first report's values.")
        # --- End of Aggregation Logic ---

    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names):
        with self.aggregation_lock:
            self.expected_du_ids = {node_config['id'] for node_config in e2_node_configurations}
            print(f"xApp will expect reports from {len(self.expected_du_ids)} DUs: {sorted(list(self.expected_du_ids))}")

        report_period = 1000 # ms - How often DU sends RIC Report (E2 Action)
        granul_period = 1000 # ms - Granularity of measurement collection at DU (E2 Action)

        for node_config in e2_node_configurations:
            e2_node_id = node_config['id']
            qos_class = node_config['qos']
            cpu_allocation = node_config['cpus']

            self.e2_node_custom_configs[e2_node_id] = {'qos': qos_class, 'cpus': cpu_allocation}

            print(f"\nProcessing subscriptions for E2 Node ID: {e2_node_id}")
            print(f"  User-defined QoS Class: {qos_class}")
            print(f"  User-defined CPU Allocation: {cpu_allocation}")

            current_ue_id_for_style2 = ue_ids_config[0] if ue_ids_config else None
            
            subscription_callback = lambda agent, sub, hdr, msg, ue_id_bound=current_ue_id_for_style2: \
                self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, ue_id_bound if kpm_report_style == 2 else None)

            # Common logic for checking required metrics for aggregation
            if kpm_report_style in [1, 2]:
                if not (self.metric_name_dl_thp in metric_names and self.metric_name_ul_thp in metric_names):
                    print(f"Warning: For aggregation with style {kpm_report_style} on {e2_node_id}, expecting metrics '{self.metric_name_dl_thp}' and '{self.metric_name_ul_thp}'. "
                          f"Ensure they are in --metrics: {metric_names}")

            if kpm_report_style == 1:
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 1, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, metric_names, granul_period, subscription_callback)
            elif kpm_report_style == 2:
                if not ue_ids_config:
                    print(f"ERROR: KPM Report Style 2 requires at least one UE ID. Skipping subscription for E2 node {e2_node_id}.")
                    continue
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 2, UE_id: {current_ue_id_for_style2}, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_2(e2_node_id, report_period, current_ue_id_for_style2, metric_names, granul_period, subscription_callback)
            elif kpm_report_style == 3:
                current_metrics_style3 = metric_names
                if len(metric_names) > 1:
                    current_metrics_style3 = metric_names[0]
                    print(f"INFO: For E2 Node {e2_node_id}, Style 3: only 1 metric can be requested, selected metric: {current_metrics_style3}")
                matchingConds = [{'matchingCondChoice': ('testCondInfo', {'testType': ('ul-rSRP', 'true'), 'testExpr': 'lessthan', 'testValue': ('valueInt', 1000)})}]
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 3, metrics: {[current_metrics_style3] if isinstance(current_metrics_style3, str) else current_metrics_style3}")
                self.e2sm_kpm.subscribe_report_service_style_3(e2_node_id, report_period, matchingConds, [current_metrics_style3] if isinstance(current_metrics_style3, str) else current_metrics_style3, granul_period, subscription_callback)
            elif kpm_report_style == 4:
                matchingUeConds = [{'testCondInfo': {'testType': ('ul-rSRP', 'true'), 'testExpr': 'lessthan', 'testValue': ('valueInt', 1000)}}]
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 4, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_4(e2_node_id, report_period, matchingUeConds, metric_names, granul_period, subscription_callback)
            elif kpm_report_style == 5:
                current_ue_ids_for_style5 = list(ue_ids_config)
                if len(current_ue_ids_for_style5) < 2:
                    if not current_ue_ids_for_style5: dummyUeId1, dummyUeId2 = 1, 2; current_ue_ids_for_style5.extend([dummyUeId1, dummyUeId2])
                    else: dummyUeId = current_ue_ids_for_style5[0] + 1; current_ue_ids_for_style5.append(dummyUeId)
                    print(f"INFO: For E2 Node {e2_node_id}, Style 5 requires at least two UE IDs. Adjusted UE_ids: {current_ue_ids_for_style5}")
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 5, UE_ids: {current_ue_ids_for_style5}, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_5(e2_node_id, report_period, current_ue_ids_for_style5, metric_names, granul_period, subscription_callback)
            else:
                print(f"INFO: Subscription for E2SM_KPM Report Service Style {kpm_report_style} is not supported for E2 node {e2_node_id}")

    def signal_handler(self, signum, frame):
        print(f"Signal {signum} received, xApp is shutting down...")
        self._stop_event.set() # Signal the timer thread to stop
        if self.processing_timer:
            self.processing_timer.cancel() # Attempt to cancel the timer
        
        # Call the superclass's signal_handler for its cleanup (e.g., RMR deregister)
        # Make sure this matches how your xAppBase expects to be called
        if hasattr(super(), 'signal_handler') and callable(super().signal_handler):
            super().signal_handler(signum, frame)
        else:
            print("xAppBase does not have a callable signal_handler or not using Python 3 super().")
            # Perform necessary base cleanup manually if super() doesn't work or is not Python 3 style
            # For example, if xAppBase provides a specific shutdown method:
            # self.shutdown_rmr() # or similar
            sys.exit(0) # Exit if superclass doesn't handle it


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp with KPM aggregation')
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")

    # E2 Node related arguments
    parser.add_argument("--e2_node_ids", type=str, required=True, help="Comma-separated list of E2 Node IDs (e.g., 'du1,du2')")
    parser.add_argument("--qos_classes", type=int, nargs='+', required=True, help="List of QoS classes (1-4) for each E2 Node, space-separated (e.g., 1 2)")
    parser.add_argument("--cpu_allocations", type=str, nargs='+', required=True, help="List of CPU allocations for each E2 Node, space-separated. Each allocation is a string like '0-2,5' or '3' (e.g., '0-1' '2,3')")

    # TDP limits
    parser.add_argument("--tdp_min_watts", type=float, required=True, help="Minimum desired TDP limit in Watts (e.g., 100.0)")
    parser.add_argument("--tdp_max_watts", type=float, required=True, help="Maximum desired TDP limit in Watts (e.g., 150.0)")

    # Subscription parameters
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID (for E2SM KPM service model)")
    parser.add_argument("--kpm_report_style", type=int, default=1, choices=range(1,6), help="KPM Report Style (1-5)")
    parser.add_argument("--ue_ids", type=str, default='', help="Comma-separated list of UE IDs (relevant for styles 2, 5, e.g., '0,1')")
    parser.add_argument("--metrics", type=str, default='DRB.UEThpDl,DRB.UEThpUl', help="Comma-separated list of Metrics names (e.g., 'Metric1,Metric2')")

    # Aggregation control
    parser.add_argument("--agg_check_interval", type=float, default=1.0, help="Interval (seconds) to check for KPM aggregation completion.")
    parser.add_argument("--agg_grace_period", type=int, default=500, help="Grace period (milliseconds) after interval end to wait for late KPM reports.")

    args = parser.parse_args()

    # Validate TDP limits
    if args.tdp_min_watts < 0 or args.tdp_max_watts < 0:
        print(f"Error: TDP limits must be non-negative.")
        sys.exit(1)
    if args.tdp_min_watts > args.tdp_max_watts:
        print(f"Error: TDP min watts ({args.tdp_min_watts}) cannot be greater than TDP max watts ({args.tdp_max_watts}).")
        sys.exit(1)

    # Parse and validate E2 Node specific configurations
    e2_node_ids_list = [node_id.strip() for node_id in args.e2_node_ids.split(",") if node_id.strip()]
    qos_classes_list = args.qos_classes
    cpu_allocations_str_list = args.cpu_allocations

    if not (len(e2_node_ids_list) == len(qos_classes_list) == len(cpu_allocations_str_list)):
        print("Error: The number of e2_node_ids, qos_classes, and cpu_allocations must match.")
        print(f"  E2 Node IDs ({len(e2_node_ids_list)}): {e2_node_ids_list}")
        print(f"  QoS Classes ({len(qos_classes_list)}): {qos_classes_list}")
        print(f"  CPU Allocations ({len(cpu_allocations_str_list)}): {cpu_allocations_str_list}")
        sys.exit(1)

    e2_node_configurations = []
    for i in range(len(e2_node_ids_list)):
        node_id = e2_node_ids_list[i]
        qos = qos_classes_list[i]
        cpu_str = cpu_allocations_str_list[i]

        if not (1 <= qos <= 4):
            print(f"Error: QoS class for E2 Node '{node_id}' must be between 1 and 4, got {qos}.")
            sys.exit(1)

        parsed_cpus = parse_cpu_allocation(cpu_str)
        if parsed_cpus is None: # parse_cpu_allocation prints its own error
            sys.exit(1)
        
        e2_node_configurations.append({'id': node_id, 'qos': qos, 'cpus': parsed_cpus})

    if not e2_node_configurations:
        print("Error: No E2 Node configurations provided or parsed successfully.")
        sys.exit(1)

    config_path = args.config # Renamed for clarity
    ran_func_id = args.ran_func_id
    ue_ids_list = list(map(int, args.ue_ids.split(","))) if args.ue_ids.strip() else []
    kpm_report_style = args.kpm_report_style
    metrics_list = [metric.strip() for metric in args.metrics.split(",") if metric.strip()]
    
    # Ensure required metrics for aggregation are present if style 1 or 2
    if kpm_report_style in [1, 2]:
        required_metrics_for_agg = {"DRB.UEThpDl", "DRB.UEThpUl"}
        if not required_metrics_for_agg.issubset(set(metrics_list)):
            print(f"Warning: For KPM aggregation with style {kpm_report_style}, metrics {list(required_metrics_for_agg)} are expected.")
            print(f"         Current metrics: {metrics_list}. Aggregation for these specific throughputs might not work as intended.")
            # You might want to automatically add them:
            # for m_req in required_metrics_for_agg:
            #     if m_req not in metrics_list:
            #         metrics_list.append(m_req)
            # print(f"         Adjusted metrics list to include required for aggregation: {metrics_list}")


    # Create MyXapp instance
    myXapp = MyXapp(config_path, args.http_server_port, args.rmr_port, 
                    args.tdp_min_watts, args.tdp_max_watts,
                    args.agg_check_interval, args.agg_grace_period)
    
    # This assumes e2sm_kpm is an attribute initialized by xAppBase or MyXapp's super().__init__
    if hasattr(myXapp, 'e2sm_kpm') and myXapp.e2sm_kpm is not None:
        myXapp.e2sm_kpm.set_ran_func_id(ran_func_id)
    else:
        print("Warning: myXapp.e2sm_kpm is not available. Cannot set RAN function ID.")
        # This might be an issue if e2sm_kpm is not initialized by the base class as expected

    # Connect exit signals
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with the following E2 Node configurations:")
    for cfg_item in e2_node_configurations:
        print(f"  - ID: {cfg_item['id']}, QoS: {cfg_item['qos']}, CPUs: {cfg_item['cpus']}")
    print(f"Global KPM Style: {kpm_report_style}, Metrics: {metrics_list}, UE IDs (for relevant styles): {ue_ids_list}")
    print(f"Aggregation Check Interval: {args.agg_check_interval}s, Grace Period: {args.agg_grace_period}ms")
    
    # Start xApp's main logic (which includes starting RMR listeners etc. via decorator)
    myXapp.start(e2_node_configurations, kpm_report_style, ue_ids_list, metrics_list)
    
    # Keep the main thread alive for daemon threads (like the timer)
    try:
        while not myXapp._stop_event.is_set():
            time.sleep(1) # Keep main thread responsive to signals
    except KeyboardInterrupt:
        print("\nMain thread: KeyboardInterrupt received, initiating shutdown via signal handler.")
        # The signal handler should already be triggered by Ctrl+C.
        # If it wasn't (e.g. some specific terminal issue), this ensures cleanup.
        if not myXapp._stop_event.is_set():
            myXapp.signal_handler(signal.SIGINT, None) 
    finally:
        # Wait for the timer thread to finish if it's joinable and not a daemon,
        # or just ensure _stop_event is set.
        if myXapp.processing_timer and myXapp.processing_timer.is_alive():
            print("Main thread: Waiting for processing timer to complete...")
            # myXapp.processing_timer.join(timeout=2) # Optional: wait for timer to finish its current run
        print("xApp main loop finished or xApp shutting down.")
