#!/usr/bin/env python3
import sys
import argparse
import signal
from lib.xAppBase import xAppBase # Assuming this is your xApp base library
# Make sure your e2sm_kpm_module is importable, e.g. from lib.e2sm_kpm_module import e2sm_kpm_module
# For this example, I'll assume it's part of xAppBase or handled internally by it.
from datetime import datetime
import re # For parsing CPU ranges
import threading # For locking
from collections import defaultdict # For easier aggregation data structure

# Helper function to parse CPU allocation strings (remains the same)
def parse_cpu_allocation(cpu_str):
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
    def __init__(self, config, http_server_port, rmr_port, tdp_min_watts, tdp_max_watts):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)

        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = f"/mnt/data/xapp/xapp_{self.timestamp}.txt"
        print(f"Logging RIC Indication data to: {self.log_file}")

        self.tdp_min_watts = tdp_min_watts
        self.tdp_max_watts = tdp_max_watts
        print(f"Desired TDP Limit Range: {self.tdp_min_watts}W - {self.tdp_max_watts}W")
        self.e2_node_custom_configs = {}

        # For aggregation
        self.aggregation_windows = defaultdict(lambda: {
            "total_dl_throughput": 0.0,
            "total_ul_throughput": 0.0,
            "reported_nodes": set()
        })
        self.subscribed_e2_node_ids = set() # Will be populated in start()
        self.aggregation_lock = threading.Lock()
        self.subscribed_metrics_for_agg = [] # Will be populated in start()
        self.TARGET_METRICS_FOR_AGGREGATION = {"DRB.UEThpDl", "DRB.UEThpUl"}


    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id):
        # Standard logging for the indication
        if kpm_report_style == 2:
            print(f"\nRIC Indication Received from {e2_agent_id} for Subscription ID: {subscription_id}, KPM Report Style: {kpm_report_style}, UE ID: {ue_id}")
        else:
            print(f"\nRIC Indication Received from {e2_agent_id} for Subscription ID: {subscription_id}, KPM Report Style: {kpm_report_style}")

        try:
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr) # This should return {'colletStartTime': datetime_obj, ...}
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            print(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}")
            with open(self.log_file, "a", buffering=1) as file_obj:
                file_obj.write(f"[{datetime.now()}] ERROR processing indication from {e2_agent_id} for sub {subscription_id}: {e}\n")
                file_obj.write(f"Header: {indication_hdr}\n") # Log raw header
                file_obj.write(f"Message: {indication_msg}\n") # Log raw message
            return

        # Log detailed KPM content to file (existing logic)
        def redirect_output_to_file(output, file_obj):
            file_obj.write(output + "\n")
            file_obj.flush()

        with open(self.log_file, "a", buffering=1) as file_obj:
            timestamp_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            log_prefix = f"[{timestamp_log}] [DU: {e2_agent_id}] [SubID: {subscription_id}]"
            redirect_output_to_file(f"{log_prefix} E2SM_KPM RIC Indication Content:", file_obj)
            # ... (rest of your detailed file logging) ...
            collet_start_time = indication_hdr_extracted.get('colletStartTime', 'N/A')
            redirect_output_to_file(f"{log_prefix} -ColletStartTime: {collet_start_time}", file_obj) # Already a datetime if extracted correctly
            # ... (continue logging other fields as before) ...


        # --- Data Aggregation Logic ---
        # Check if this callback is for KPM style 1 and if we subscribed to the target metrics
        perform_aggregation = False
        if kpm_report_style == 1 and self.TARGET_METRICS_FOR_AGGREGATION.issubset(set(self.subscribed_metrics_for_agg)):
            perform_aggregation = True

        if perform_aggregation:
            collet_start_time_dt = indication_hdr_extracted.get('colletStartTime')

            if collet_start_time_dt and isinstance(collet_start_time_dt, datetime):
                current_dl = 0.0
                current_ul = 0.0

                if "measData" in meas_data:
                    # Assuming metrics are like "DRB.UEThpDl": [value]
                    dl_values = meas_data["measData"].get("DRB.UEThpDl")
                    if dl_values and isinstance(dl_values, list) and len(dl_values) > 0:
                        try:
                            current_dl = float(dl_values[0])
                        except (ValueError, TypeError):
                            print(f"Warning: Aggregation: Could not convert DRB.UEThpDl value '{dl_values[0]}' to float for DU {e2_agent_id} at {collet_start_time_dt}")
                    
                    ul_values = meas_data["measData"].get("DRB.UEThpUl")
                    if ul_values and isinstance(ul_values, list) and len(ul_values) > 0:
                        try:
                            current_ul = float(ul_values[0])
                        except (ValueError, TypeError):
                            print(f"Warning: Aggregation: Could not convert DRB.UEThpUl value '{ul_values[0]}' to float for DU {e2_agent_id} at {collet_start_time_dt}")
                else:
                    print(f"Warning: Aggregation: 'measData' not found in KPM Style 1 report from DU {e2_agent_id} at {collet_start_time_dt}")


                with self.aggregation_lock:
                    agg_window = self.aggregation_windows[collet_start_time_dt]
                    agg_window["total_dl_throughput"] += current_dl
                    agg_window["total_ul_throughput"] += current_ul
                    agg_window["reported_nodes"].add(e2_agent_id)

                    # Check if all subscribed nodes have reported for this window
                    if len(self.subscribed_e2_node_ids) > 0 and \
                       len(agg_window["reported_nodes"]) == len(self.subscribed_e2_node_ids):
                        
                        # All nodes reported, print totals and clear window
                        print(f"\n--- AGGREGATED TOTALS for ColletStartTime: {collet_start_time_dt.strftime('%Y-%m-%d %H:%M:%S')} ---")
                        print(f"  Total DRB.UEThpDl: {agg_window['total_dl_throughput']:.2f}")
                        print(f"  Total DRB.UEThpUl: {agg_window['total_ul_throughput']:.2f}")
                        print(f"  Reported from DUs: {sorted(list(agg_window['reported_nodes']))}")
                        print(f"------------------------------------------------------------------")
                        
                        del self.aggregation_windows[collet_start_time_dt] # Clean up processed window
            else:
                if kpm_report_style == 1 : # Only warn if we expected to aggregate
                    print(f"Warning: Aggregation: colletStartTime missing or not datetime for DU {e2_agent_id}.")
        # --- End Data Aggregation Logic ---


    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names):
        report_period = 1000 # ms
        granul_period = 1000 # ms

        # Populate the set of all E2 node IDs we are subscribing to and metrics for aggregation check
        self.subscribed_metrics_for_agg = list(metric_names) # Store the actual list of subscribed metrics
        for node_config in e2_node_configurations:
            self.subscribed_e2_node_ids.add(node_config['id'])

        print(f"xApp will attempt to aggregate for {len(self.subscribed_e2_node_ids)} DUs: {self.subscribed_e2_node_ids}")
        if not self.TARGET_METRICS_FOR_AGGREGATION.issubset(set(self.subscribed_metrics_for_agg)):
            print(f"Warning: Not all target metrics for aggregation ({self.TARGET_METRICS_FOR_AGGREGATION}) are in the subscribed metrics list ({self.subscribed_metrics_for_agg}). Aggregation might not work as expected.")


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

            # ... (rest of your subscription logic for different KPM styles remains the same) ...
            if kpm_report_style == 1:
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 1, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, metric_names, granul_period, subscription_callback)
            # ... (elif for other styles) ...
            else:
                print(f"INFO: Subscription for E2SM_KPM Report Service Style {kpm_report_style} is not supported for E2 node {e2_node_id}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
    # ... (all your argparse arguments remain the same) ...
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")
    parser.add_argument("--e2_node_ids", type=str, required=True, help="Comma-separated list of E2 Node IDs (e.g., 'du1,du2')")
    parser.add_argument("--qos_classes", type=int, nargs='+', required=True, help="List of QoS classes (1-4) for each E2 Node, space-separated (e.g., 1 2)")
    parser.add_argument("--cpu_allocations", type=str, nargs='+', required=True, help="List of CPU allocations for each E2 Node, space-separated. Each allocation is a string like '0-2,5' or '3' (e.g., '0-1' '2,3')")
    parser.add_argument("--tdp_min_watts", type=float, required=True, help="Minimum desired TDP limit in Watts (e.g., 100.0)")
    parser.add_argument("--tdp_max_watts", type=float, required=True, help="Maximum desired TDP limit in Watts (e.g., 150.0)")
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID (for E2SM KPM service model)")
    parser.add_argument("--kpm_report_style", type=int, default=1, choices=range(1,6), help="KPM Report Style (1-5)")
    parser.add_argument("--ue_ids", type=str, default='', help="Comma-separated list of UE IDs (relevant for styles 2, 5, e.g., '0,1')")
    parser.add_argument("--metrics", type=str, default='DRB.UEThpDl,DRB.UEThpUl', help="Comma-separated list of Metrics names (e.g., 'Metric1,Metric2')")


    args = parser.parse_args()

    # ... (rest of your argument parsing and validation logic remains the same) ...
    if args.tdp_min_watts > args.tdp_max_watts:
        print(f"Error: TDP min watts ({args.tdp_min_watts}) cannot be greater than TDP max watts ({args.tdp_max_watts}).")
        sys.exit(1)
    if args.tdp_min_watts < 0 or args.tdp_max_watts < 0:
        print(f"Error: TDP limits must be non-negative.")
        sys.exit(1)

    e2_node_ids_list_str = [node_id.strip() for node_id in args.e2_node_ids.split(",") if node_id.strip()]
    qos_classes_list = args.qos_classes
    cpu_allocations_str_list = args.cpu_allocations

    if not (len(e2_node_ids_list_str) == len(qos_classes_list) == len(cpu_allocations_str_list)):
        print("Error: The number of e2_node_ids, qos_classes, and cpu_allocations must match.")
        sys.exit(1)

    e2_node_configurations = []
    for i in range(len(e2_node_ids_list_str)):
        node_id = e2_node_ids_list_str[i]
        qos = qos_classes_list[i]
        cpu_str = cpu_allocations_str_list[i]
        if not (1 <= qos <= 4):
            print(f"Error: QoS class for E2 Node '{node_id}' must be between 1 and 4, got {qos}.")
            sys.exit(1)
        parsed_cpus = parse_cpu_allocation(cpu_str)
        if parsed_cpus is None:
            print(f"Error: Invalid CPU allocation format '{cpu_str}' for E2 Node '{node_id}'. Exiting.")
            sys.exit(1)
        e2_node_configurations.append({'id': node_id, 'qos': qos, 'cpus': parsed_cpus})

    if not e2_node_configurations:
        print("Error: No E2 Node configurations provided or parsed successfully.")
        sys.exit(1)

    config = args.config
    ran_func_id = args.ran_func_id
    ue_ids_list = list(map(int, args.ue_ids.split(","))) if args.ue_ids else []
    kpm_report_style = args.kpm_report_style
    metrics_list = [metric.strip() for metric in args.metrics.split(",") if metric.strip()]
    if not ("DRB.UEThpDl" in metrics_list and "DRB.UEThpUl" in metrics_list) and kpm_report_style == 1:
        print("Warning: For aggregation, 'DRB.UEThpDl' and 'DRB.UEThpUl' should be in the --metrics list when using KPM Style 1.")
        # You might want to add them automatically if style is 1 or make them mandatory for this feature.


    myXapp = MyXapp(config, args.http_server_port, args.rmr_port, args.tdp_min_watts, args.tdp_max_watts)
    myXapp.e2sm_kpm.set_ran_func_id(ran_func_id)

    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with the following E2 Node configurations:")
    for cfg in e2_node_configurations:
        print(f"  - ID: {cfg['id']}, QoS: {cfg['qos']}, CPUs: {cfg['cpus']}")
    print(f"Global KPM Style: {kpm_report_style}, Metrics: {metrics_list}, UE IDs (for relevant styles): {ue_ids_list}")
    
    myXapp.start(e2_node_configurations, kpm_report_style, ue_ids_list, metrics_list)
