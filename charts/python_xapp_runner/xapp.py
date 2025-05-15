#!/usr/bin/env python3
import sys
import argparse
import signal
from lib.xAppBase import xAppBase # Assuming this is your xApp base library
from datetime import datetime
import re # For parsing CPU ranges

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
    def __init__(self, config, http_server_port, rmr_port, tdp_min_watts, tdp_max_watts): # Added TDP args
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)

        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = f"/mnt/data/xapp/xapp_{self.timestamp}.txt"
        print(f"Logging RIC Indication data to: {self.log_file}")

        self.tdp_min_watts = tdp_min_watts
        self.tdp_max_watts = tdp_max_watts
        print(f"Desired TDP Limit Range: {self.tdp_min_watts}W - {self.tdp_max_watts}W")
        # These will be used later by your logic
        self.e2_node_custom_configs = {} # To store QoS/CPU info if needed later keyed by e2_node_id

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id):
        if kpm_report_style == 2:
            print(f"\nRIC Indication Received from {e2_agent_id} for Subscription ID: {subscription_id}, KPM Report Style: {kpm_report_style}, UE ID: {ue_id}")
        else:
            print(f"\nRIC Indication Received from {e2_agent_id} for Subscription ID: {subscription_id}, KPM Report Style: {kpm_report_style}")

        try:
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            print(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}")
            with open(self.log_file, "a", buffering=1) as file_obj:
                file_obj.write(f"[{datetime.now()}] ERROR processing indication from {e2_agent_id} for sub {subscription_id}: {e}\n")
                file_obj.write(f"Header: {indication_hdr}\n")
                file_obj.write(f"Message: {indication_msg}\n")
            return

        def redirect_output_to_file(output, file_obj):
            file_obj.write(output + "\n")
            file_obj.flush()

        with open(self.log_file, "a", buffering=1) as file_obj:
            timestamp_log = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            log_prefix = f"[{timestamp_log}] [DU: {e2_agent_id}] [SubID: {subscription_id}]"

            redirect_output_to_file(f"{log_prefix} E2SM_KPM RIC Indication Content:", file_obj)
            print(f"{log_prefix} E2SM_KPM RIC Indication Content:")

            collet_start_time = indication_hdr_extracted.get('colletStartTime', 'N/A')
            redirect_output_to_file(f"{log_prefix} -ColletStartTime: {collet_start_time}", file_obj)
            print(f"-ColletStartTime: {collet_start_time}")

            redirect_output_to_file(f"{log_prefix} -Measurements Data:", file_obj)
            print("-Measurements Data:")

            granulPeriod = meas_data.get("granulPeriod", None)
            if granulPeriod is not None:
                redirect_output_to_file(f"{log_prefix} -granulPeriod: {granulPeriod}", file_obj)
                print(f"-granulPeriod: {granulPeriod}")

            if kpm_report_style in [1, 2]:
                if "measData" in meas_data:
                    for metric_name, value in meas_data["measData"].items():
                        redirect_output_to_file(f"{log_prefix} --Metric: {metric_name}, Value: {value}", file_obj)
                        print(f"--Metric: {metric_name}, Value: {value}")
                else:
                    redirect_output_to_file(f"{log_prefix} --No 'measData' found in KPM Style {kpm_report_style} report.", file_obj)
                    print(f"--No 'measData' found in KPM Style {kpm_report_style} report.")
            else:
                if "ueMeasData" in meas_data:
                    for ue_id_report, ue_meas_data in meas_data["ueMeasData"].items():
                        redirect_output_to_file(f"{log_prefix} --UE_id: {ue_id_report}", file_obj)
                        print(f"--UE_id: {ue_id_report}")
                        granulPeriod_ue = ue_meas_data.get("granulPeriod", None)
                        if granulPeriod_ue is not None:
                            redirect_output_to_file(f"{log_prefix} ---granulPeriod: {granulPeriod_ue}", file_obj)
                            print(f"---granulPeriod: {granulPeriod_ue}")
                        if "measData" in ue_meas_data:
                            for metric_name, value in ue_meas_data["measData"].items():
                                redirect_output_to_file(f"{log_prefix} ---Metric: {metric_name}, Value: {value}", file_obj)
                                print(f"---Metric: {metric_name}, Value: {value}")
                        else:
                            redirect_output_to_file(f"{log_prefix} ---No 'measData' found for UE {ue_id_report}.", file_obj)
                            print(f"---No 'measData' found for UE {ue_id_report}.")
                else:
                    redirect_output_to_file(f"{log_prefix} --No 'ueMeasData' found in KPM Style {kpm_report_style} report.", file_obj)
                    print(f"--No 'ueMeasData' found in KPM Style {kpm_report_style} report.")

    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names):
        # e2_node_configurations is a list of dicts: [{'id': str, 'qos': int, 'cpus': list[int]}, ...]
        report_period = 1000
        granul_period = 1000

        for node_config in e2_node_configurations:
            e2_node_id = node_config['id']
            qos_class = node_config['qos']
            cpu_allocation = node_config['cpus']

            # Store this config for later use if your xApp logic needs it
            self.e2_node_custom_configs[e2_node_id] = {'qos': qos_class, 'cpus': cpu_allocation}

            print(f"\nProcessing subscriptions for E2 Node ID: {e2_node_id}")
            print(f"  User-defined QoS Class: {qos_class}")
            print(f"  User-defined CPU Allocation: {cpu_allocation}")
            # You will use qos_class and cpu_allocation in your subsequent logic for this DU.

            current_ue_id_for_style2 = ue_ids_config[0] if ue_ids_config else None
            subscription_callback = lambda agent, sub, hdr, msg, ue_id_bound=current_ue_id_for_style2: \
                self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, ue_id_bound if kpm_report_style == 2 else None)

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
                current_metrics = metric_names
                if len(metric_names) > 1:
                    current_metrics = metric_names[0]
                    print(f"INFO: For E2 Node {e2_node_id}, Style 3: only 1 metric can be requested, selected metric: {current_metrics}")
                matchingConds = [{'matchingCondChoice': ('testCondInfo', {'testType': ('ul-rSRP', 'true'), 'testExpr': 'lessthan', 'testValue': ('valueInt', 1000)})}]
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 3, metrics: {current_metrics}")
                self.e2sm_kpm.subscribe_report_service_style_3(e2_node_id, report_period, matchingConds, [current_metrics] if isinstance(current_metrics, str) else current_metrics, granul_period, subscription_callback)
            elif kpm_report_style == 4:
                matchingUeConds = [{'testCondInfo': {'testType': ('ul-rSRP', 'true'), 'testExpr': 'lessthan', 'testValue': ('valueInt', 1000)}}]
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 4, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_4(e2_node_id, report_period, matchingUeConds, metric_names, granul_period, subscription_callback)
            elif kpm_report_style == 5:
                current_ue_ids_for_style5 = list(ue_ids_config)
                if len(current_ue_ids_for_style5) < 2:
                    if not current_ue_ids_for_style5:
                        dummyUeId1, dummyUeId2 = 1, 2
                        current_ue_ids_for_style5.extend([dummyUeId1, dummyUeId2])
                        print(f"INFO: For E2 Node {e2_node_id}, Style 5 requires at least two UE IDs. Added dummy UeIDs: {dummyUeId1}, {dummyUeId2}")
                    else:
                        dummyUeId = current_ue_ids_for_style5[0] + 1
                        current_ue_ids_for_style5.append(dummyUeId)
                        print(f"INFO: For E2 Node {e2_node_id}, Style 5 requires at least two UE IDs. Added dummy UeID: {dummyUeId}")
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 5, UE_ids: {current_ue_ids_for_style5}, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_5(e2_node_id, report_period, current_ue_ids_for_style5, metric_names, granul_period, subscription_callback)
            else:
                print(f"INFO: Subscription for E2SM_KPM Report Service Style {kpm_report_style} is not supported for E2 node {e2_node_id}")

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
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

    # Existing subscription parameters
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID (for E2SM KPM service model)")
    parser.add_argument("--kpm_report_style", type=int, default=1, choices=range(1,6), help="KPM Report Style (1-5)")
    parser.add_argument("--ue_ids", type=str, default='', help="Comma-separated list of UE IDs (relevant for styles 2, 5, e.g., '0,1')")
    parser.add_argument("--metrics", type=str, default='DRB.UEThpUl,DRB.UEThpDl', help="Comma-separated list of Metrics names (e.g., 'Metric1,Metric2')")

    args = parser.parse_args()

    # Validate TDP limits
    if args.tdp_min_watts > args.tdp_max_watts:
        print(f"Error: TDP min watts ({args.tdp_min_watts}) cannot be greater than TDP max watts ({args.tdp_max_watts}).")
        sys.exit(1)
    if args.tdp_min_watts < 0 or args.tdp_max_watts < 0:
        print(f"Error: TDP limits must be non-negative.")
        sys.exit(1)

    # Parse and validate E2 Node specific configurations
    e2_node_ids_list = [node_id.strip() for node_id in args.e2_node_ids.split(",") if node_id.strip()]
    qos_classes_list = args.qos_classes
    cpu_allocations_str_list = args.cpu_allocations

    if not (len(e2_node_ids_list) == len(qos_classes_list) == len(cpu_allocations_str_list)):
        print("Error: The number of e2_node_ids, qos_classes, and cpu_allocations must match.")
        print(f"  E2 Node IDs count: {len(e2_node_ids_list)}")
        print(f"  QoS Classes count: {len(qos_classes_list)}")
        print(f"  CPU Allocations count: {len(cpu_allocations_str_list)}")
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

    # Create MyXapp.
    myXapp = MyXapp(config, args.http_server_port, args.rmr_port, args.tdp_min_watts, args.tdp_max_watts)
    myXapp.e2sm_kpm.set_ran_func_id(ran_func_id) # Assuming same RAN func ID for all KPM interactions

    # Connect exit signals.
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with the following E2 Node configurations:")
    for cfg in e2_node_configurations:
        print(f"  - ID: {cfg['id']}, QoS: {cfg['qos']}, CPUs: {cfg['cpus']}")
    print(f"Global KPM Style: {kpm_report_style}, Metrics: {metrics_list}, UE IDs (for relevant styles): {ue_ids_list}")
    
    # Start xApp.
    myXapp.start(e2_node_configurations, kpm_report_style, ue_ids_list, metrics_list)
