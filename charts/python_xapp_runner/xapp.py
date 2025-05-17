#!/usr/bin/env python3
import sys
import argparse
import signal
from lib.xAppBase import xAppBase
from datetime import datetime
import re
import threading
from collections import defaultdict
import logging

# Helper function to parse CPU allocation strings
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
                    return None
                start_str, end_str = part.split('-', 1)
                start = int(start_str)
                end = int(end_str)
                if start > end:
                    return None
                cpus.update(range(start, end + 1))
            except ValueError:
                return None
        else:
            try:
                cpus.add(int(part))
            except ValueError:
                return None
    return sorted(list(cpus))

class MyXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port, tdp_min_watts, tdp_max_watts, log_level=logging.INFO):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)

        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file_path = f"/mnt/data/xapp/xapp_{self.timestamp}.txt"

        self.logger = logging.getLogger(f"MyXapp_{self.timestamp}")
        self.logger.setLevel(log_level)
        fh = logging.FileHandler(self.log_file_path, mode='a')
        fh.setLevel(log_level)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        fh.setFormatter(formatter)
        self.logger.addHandler(fh)
        self.logger.propagate = False

        self.logger.info(f"Logging RIC Indication data to: {self.log_file_path}")
        print(f"Logging RIC Indication data to: {self.log_file_path}")

        self.tdp_min_watts = tdp_min_watts
        self.tdp_max_watts = tdp_max_watts
        self.logger.info(f"Desired TDP Limit Range: {self.tdp_min_watts}W - {self.tdp_max_watts}W")
        self.e2_node_custom_configs = {}

        self.aggregation_windows = defaultdict(lambda: {
            "total_dl_throughput": 0.0,
            "total_ul_throughput": 0.0,
            "reported_nodes": set(),
            "qos_dl_throughput": defaultdict(float),
            "qos_ul_throughput": defaultdict(float)
        })
        self.subscribed_e2_node_ids = set()
        self.aggregation_lock = threading.Lock()
        self.subscribed_metrics_for_agg = [] # This will store all subscribed metrics
        self.THROUGHPUT_METRICS_FOR_AGGREGATION = {"DRB.UEThpDl", "DRB.UEThpUl"} # Specific for summing

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id):
        try:
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            self.logger.error(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}", exc_info=True)
            self.logger.debug(f"Failed Header for {e2_agent_id}: {indication_hdr}")
            self.logger.debug(f"Failed Message for {e2_agent_id}: {indication_msg}")
            return

        if self.logger.isEnabledFor(logging.DEBUG):
            log_prefix = f"[DU: {e2_agent_id}] [SubID: {subscription_id}]"
            self.logger.debug(f"{log_prefix} E2SM_KPM RIC Indication Content:")
            collet_start_time_log = indication_hdr_extracted.get('colletStartTime', 'N/A')
            self.logger.debug(f"{log_prefix} -ColletStartTime: {collet_start_time_log}")
            self.logger.debug(f"{log_prefix} -Measurements Data:")
            granulPeriod = meas_data.get("granulPeriod", None)
            if granulPeriod is not None:
                self.logger.debug(f"{log_prefix} -granulPeriod: {granulPeriod}")
            
            # KPM Style 1: Cell-level metrics directly in measData
            if kpm_report_style == 1:
                if "measData" in meas_data:
                    for metric_name, value in meas_data["measData"].items():
                        # The value for DRB.AirIfDelayDist might be a list or dict
                        self.logger.debug(f"{log_prefix} --Metric: {metric_name}, Value: {value}")
                else:
                    self.logger.debug(f"{log_prefix} --No 'measData' found in KPM Style 1 report.")
            
            # KPM Style 2: Test-Cond based, also has measData for a specific metric (usually one)
            elif kpm_report_style == 2:
                if "measData" in meas_data: # According to your library, Style 2 also has 'measData' for the single subscribed metric
                    for metric_name, value in meas_data["measData"].items():
                         self.logger.debug(f"{log_prefix} --Metric (Style 2): {metric_name}, Value: {value}")
                else:
                    self.logger.debug(f"{log_prefix} --No 'measData' found in KPM Style 2 report.")
                # Style 2 might also have 'ueMeasData' in some interpretations if matchingUEidList is present and populated
                # Your library code _extract_content_ind_msg_f2 puts things into meas_data_dict[ueID]["measData"]
                # which then becomes "ueMeasData" at the top level return. Let's adjust for that.
                if "ueMeasData" in meas_data: # If your library's Style 2 parser populates this
                    for ue_id_report, ue_meas_data_item in meas_data["ueMeasData"].items():
                        self.logger.debug(f"{log_prefix} --UE_id (Style 2): {ue_id_report}")
                        if "measData" in ue_meas_data_item:
                            for metric_name, value in ue_meas_data_item["measData"].items():
                                self.logger.debug(f"{log_prefix} ---Metric: {metric_name}, Value: {value}")

            # KPM Styles 3, 4, 5: UE-level metrics within ueMeasData
            elif kpm_report_style in [3, 4, 5]:
                if "ueMeasData" in meas_data:
                    for ue_id_report, ue_meas_data_item in meas_data["ueMeasData"].items():
                        self.logger.debug(f"{log_prefix} --UE_id: {ue_id_report}")
                        granulPeriod_ue = ue_meas_data_item.get("granulPeriod", None)
                        if granulPeriod_ue is not None:
                            self.logger.debug(f"{log_prefix} ---granulPeriod: {granulPeriod_ue}")
                        if "measData" in ue_meas_data_item:
                            for metric_name, value in ue_meas_data_item["measData"].items():
                                # The value for DRB.AirIfDelayDist might be a list or dict
                                self.logger.debug(f"{log_prefix} ---Metric: {metric_name}, Value: {value}")
                        else:
                            self.logger.debug(f"{log_prefix} ---No 'measData' found for UE {ue_id_report}.")
                else:
                    self.logger.debug(f"{log_prefix} --No 'ueMeasData' found in KPM Style {kpm_report_style} report.")

        # Aggregation logic (focused on throughput metrics)
        perform_throughput_aggregation = False
        if kpm_report_style == 1 and self.THROUGHPUT_METRICS_FOR_AGGREGATION.issubset(set(self.subscribed_metrics_for_agg)):
            perform_throughput_aggregation = True

        if perform_throughput_aggregation:
            collet_start_time_dt = indication_hdr_extracted.get('colletStartTime')
            if collet_start_time_dt and isinstance(collet_start_time_dt, datetime):
                current_dl = 0.0
                current_ul = 0.0
                if "measData" in meas_data:
                    dl_values = meas_data["measData"].get("DRB.UEThpDl")
                    if dl_values and isinstance(dl_values, list) and len(dl_values) > 0:
                        try: current_dl = float(dl_values[0])
                        except (ValueError, TypeError): self.logger.warning(f"Aggregation: Could not convert DRB.UEThpDl value '{dl_values[0]}' to float for DU {e2_agent_id} at {collet_start_time_dt}")
                    ul_values = meas_data["measData"].get("DRB.UEThpUl")
                    if ul_values and isinstance(ul_values, list) and len(ul_values) > 0:
                        try: current_ul = float(ul_values[0])
                        except (ValueError, TypeError): self.logger.warning(f"Aggregation: Could not convert DRB.UEThpUl value '{ul_values[0]}' to float for DU {e2_agent_id} at {collet_start_time_dt}")
                else:
                    self.logger.warning(f"Aggregation: 'measData' not found in KPM Style 1 report from DU {e2_agent_id} for throughput aggregation at {collet_start_time_dt}")
                
                node_qos_class = self.e2_node_custom_configs.get(e2_agent_id, {}).get('qos')
                with self.aggregation_lock:
                    agg_window = self.aggregation_windows[collet_start_time_dt]
                    agg_window["total_dl_throughput"] += current_dl
                    agg_window["total_ul_throughput"] += current_ul
                    agg_window["reported_nodes"].add(e2_agent_id)
                    if node_qos_class is not None:
                        agg_window["qos_dl_throughput"][node_qos_class] += current_dl
                        agg_window["qos_ul_throughput"][node_qos_class] += current_ul

                    if len(self.subscribed_e2_node_ids) > 0 and \
                       len(agg_window["reported_nodes"]) == len(self.subscribed_e2_node_ids):
                        agg_msg_header = f"AGGREGATED TOTALS for ColletStartTime: {collet_start_time_dt.strftime('%Y-%m-%d %H:%M:%S')}"
                        self.logger.info(agg_msg_header)
                        overall_dl_msg = f"Overall Total DRB.UEThpDl: {agg_window['total_dl_throughput']:.2f}"
                        overall_ul_msg = f"Overall Total DRB.UEThpUl: {agg_window['total_ul_throughput']:.2f}"
                        self.logger.info(overall_dl_msg)
                        self.logger.info(overall_ul_msg)
                        qos_header_msg = "Per QoS Class Totals:"
                        self.logger.info(f"  --- {qos_header_msg} ---")
                        all_qos_classes_in_window = sorted(list(set(agg_window["qos_dl_throughput"].keys()) | set(agg_window["qos_ul_throughput"].keys())))
                        for qos in all_qos_classes_in_window:
                            qos_dl = agg_window["qos_dl_throughput"].get(qos, 0.0)
                            qos_ul = agg_window["qos_ul_throughput"].get(qos, 0.0)
                            qos_class_msg = f"QoS Class {qos}: DL={qos_dl:.2f}, UL={qos_ul:.2f}"
                            self.logger.info(f"    {qos_class_msg}")
                        reported_dus_msg = f"Reported from DUs: {sorted(list(agg_window['reported_nodes']))}"
                        self.logger.info(f"  {reported_dus_msg}")
                        del self.aggregation_windows[collet_start_time_dt]
            elif kpm_report_style == 1: # Only warn if we expected to aggregate throughput
                self.logger.warning(f"Aggregation: colletStartTime missing or not datetime for DU {e2_agent_id}.")

    def signal_handler(self, signum, frame):
        self.logger.info(f"Signal {signum} received, exiting xApp...")
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
        super().signal_handler(signum, frame)

    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names):
        report_period = 1000
        granul_period = 1000

        self.subscribed_metrics_for_agg = list(metric_names) # Store all subscribed metrics
        for node_config in e2_node_configurations:
            self.subscribed_e2_node_ids.add(node_config['id'])
            self.e2_node_custom_configs[node_config['id']] = {
                'qos': node_config['qos'],
                'cpus': node_config['cpus']
            }

        self.logger.info(f"xApp will attempt throughput aggregation for {len(self.subscribed_e2_node_ids)} DUs: {self.subscribed_e2_node_ids}")
        if not self.THROUGHPUT_METRICS_FOR_AGGREGATION.issubset(set(self.subscribed_metrics_for_agg)):
            self.logger.warning(f"Not all target throughput metrics for aggregation ({self.THROUGHPUT_METRICS_FOR_AGGREGATION}) are in the subscribed metrics list ({self.subscribed_metrics_for_agg}). Throughput aggregation might not work as expected.")
        
        # Check if DRB.AirIfDelayDist is being subscribed if we want to log it
        if "DRB.AirIfDelayDist" in self.subscribed_metrics_for_agg:
            self.logger.info("DRB.AirIfDelayDist is in subscribed metrics, will attempt to log its value at DEBUG level.")
        else:
            self.logger.info("DRB.AirIfDelayDist is NOT in subscribed metrics. It will not be logged.")


        for node_config in e2_node_configurations:
            e2_node_id = node_config['id']
            qos_class = self.e2_node_custom_configs[e2_node_id]['qos']
            cpu_allocation = self.e2_node_custom_configs[e2_node_id]['cpus']
            self.logger.info(f"Processing subscriptions for E2 Node ID: {e2_node_id}, QoS: {qos_class}, CPUs: {cpu_allocation}")

            current_ue_id_for_style2 = ue_ids_config[0] if ue_ids_config else None
            subscription_callback = lambda agent, sub, hdr, msg, ue_id_bound=current_ue_id_for_style2: \
                self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, ue_id_bound if kpm_report_style == 2 else None)

            if kpm_report_style == 1:
                self.logger.info(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 1, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, metric_names, granul_period, subscription_callback)
            elif kpm_report_style == 2:
                if not ue_ids_config:
                    self.logger.error(f"KPM Report Style 2 requires at least one UE ID. Skipping subscription for E2 node {e2_node_id}.")
                    continue
                self.logger.info(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 2, UE_id: {current_ue_id_for_style2}, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_2(e2_node_id, report_period, current_ue_id_for_style2, metric_names, granul_period, subscription_callback)
            # ... (add elif for styles 3, 4, 5 if you intend to support them fully with similar logging)
            # For now, assuming Style 1, 2 are primary for this example
            else:
                self.logger.info(f"Subscription for E2SM_KPM Report Service Style {kpm_report_style} is not supported for E2 node {e2_node_id}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")
    parser.add_argument("--e2_node_ids", type=str, required=True, help="Comma-separated list of E2 Node IDs")
    parser.add_argument("--qos_classes", type=int, nargs='+', required=True, help="List of QoS classes (1-4) for each E2 Node")
    parser.add_argument("--cpu_allocations", type=str, nargs='+', required=True, help="List of CPU allocations for each E2 Node")
    parser.add_argument("--tdp_min_watts", type=float, required=True, help="Minimum desired TDP limit in Watts")
    parser.add_argument("--tdp_max_watts", type=float, required=True, help="Maximum desired TDP limit in Watts")
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID")
    parser.add_argument("--kpm_report_style", type=int, default=1, choices=range(1,6), help="KPM Report Style (1-5)")
    parser.add_argument("--ue_ids", type=str, default='', help="Comma-separated list of UE IDs")
    parser.add_argument("--metrics", type=str, default='DRB.AirIfDelayDist', help="Comma-separated list of Metrics names") # Added DRB.AirIfDelayDist to default
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level")

    args = parser.parse_args()
    log_level_int = getattr(logging, args.log_level.upper(), logging.INFO)

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

    metrics_list = [metric.strip() for metric in args.metrics.split(",") if metric.strip()]
    # Check for throughput metrics if style 1
    if args.kpm_report_style == 1 and not ("DRB.UEThpDl" in metrics_list and "DRB.UEThpUl" in metrics_list):
        print("Warning: For throughput aggregation, 'DRB.UEThpDl' and 'DRB.UEThpUl' should be in the --metrics list when using KPM Style 1.")
    # Check for AirIfDelayDist if we want to log it
    if "DRB.AirIfDelayDist" not in metrics_list:
        print("Warning: 'DRB.AirIfDelayDist' is not in the --metrics list. Its value will not be logged from indications.")


    myXapp = MyXapp(args.config, args.http_server_port, args.rmr_port, args.tdp_min_watts, args.tdp_max_watts, log_level_int)
    myXapp.e2sm_kpm.set_ran_func_id(args.ran_func_id)

    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with the following E2 Node configurations:")
    for cfg in e2_node_configurations:
        print(f"  - ID: {cfg['id']}, QoS: {cfg['qos']}, CPUs: {cfg['cpus']}")
    print(f"Global KPM Style: {args.kpm_report_style}, Metrics: {metrics_list}")
    
    myXapp.start(e2_node_configurations, args.kpm_report_style, list(map(int, args.ue_ids.split(","))) if args.ue_ids else [], metrics_list)
