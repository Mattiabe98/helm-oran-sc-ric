#!/usr/bin/env python3
import sys
import argparse
import signal
from lib.xAppBase import xAppBase # Assuming this is your xApp base library
from datetime import datetime
import re
import threading
from collections import defaultdict
import logging
import math # For isnan, isinf if needed, though statistics module handles most
import statistics # For stdev

# Helper function to parse CPU allocation strings
def parse_cpu_allocation(cpu_str):
    # ... (no changes from your provided code)
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
                if part.count('-') > 1: return None
                start_str, end_str = part.split('-', 1)
                start, end = int(start_str), int(end_str)
                if start > end: return None
                cpus.update(range(start, end + 1))
            except ValueError: return None
        else:
            try: cpus.add(int(part))
            except ValueError: return None
    return sorted(list(cpus))

# Helper for stdev
def calculate_stdev(data_list):
    if len(data_list) < 2:
        return 0.0  # Or float('nan') / None if you prefer to mark it as undefined
    try:
        return statistics.stdev(data_list)
    except statistics.StatisticsError:
        # This can happen if all values are identical for some implementations,
        # though len < 2 should catch most.
        return 0.0


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
            "qos_ul_throughput": defaultdict(float),
            "qos_metric_values": defaultdict(lambda: defaultdict(list)) # For new stats: {qos: {metric_name: [values]}}
        })
        self.subscribed_e2_node_ids = set()
        self.aggregation_lock = threading.Lock()
        self.all_subscribed_metrics = [] # Stores all metrics from --metrics argument
        self.THROUGHPUT_METRICS_FOR_SUMMING = {"DRB.UEThpDl", "DRB.UEThpUl"}
        self.STATISTICAL_METRICS = {
            "DRB.RlcSduDelayDl",
            "DRB.AirIfDelayUl",
            "DRB.RlcDelayUl",
            "DRB.RlcPacketDropRateDl"
        }

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id):
        try:
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            self.logger.error(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}", exc_info=True)
            self.logger.debug(f"Failed Header for {e2_agent_id}: {indication_hdr}")
            self.logger.debug(f"Failed Message for {e2_agent_id}: {indication_msg}")
            return

        # --- DEBUG Logging of all received metrics ---
        if self.logger.isEnabledFor(logging.DEBUG):
            log_prefix = f"[DU: {e2_agent_id}] [SubID: {subscription_id}]"
            # ... (Your existing comprehensive debug logging for all styles and metrics) ...
            # (Ensure this part correctly logs all metrics for all styles as per your previous version)
            self.logger.debug(f"{log_prefix} E2SM_KPM RIC Indication Content:")
            collet_start_time_log = indication_hdr_extracted.get('colletStartTime', 'N/A')
            self.logger.debug(f"{log_prefix} -ColletStartTime: {collet_start_time_log}")
            self.logger.debug(f"{log_prefix} -Measurements Data:")
            granulPeriod = meas_data.get("granulPeriod", None)
            if granulPeriod is not None: self.logger.debug(f"{log_prefix} -granulPeriod: {granulPeriod}")
            if kpm_report_style == 1:
                if "measData" in meas_data:
                    for metric_name, value in meas_data["measData"].items(): self.logger.debug(f"{log_prefix} --Metric: {metric_name}, Value: {value}")
                else: self.logger.debug(f"{log_prefix} --No 'measData' in Style 1.")
            elif kpm_report_style == 2:
                if "ueMeasData" in meas_data: # Primary data location for Style 2 from your lib
                    for ue_id_rep, ue_data in meas_data["ueMeasData"].items():
                        self.logger.debug(f"{log_prefix} --UE_id (Style 2): {ue_id_rep}")
                        if "measData" in ue_data:
                            for metric_name, value in ue_data["measData"].items(): self.logger.debug(f"{log_prefix} ---Metric: {metric_name}, Value: {value}")
            elif kpm_report_style in [3, 4, 5]:
                if "ueMeasData" in meas_data:
                    for ue_id_rep, ue_data in meas_data["ueMeasData"].items():
                        self.logger.debug(f"{log_prefix} --UE_id: {ue_id_rep}")
                        if "measData" in ue_data:
                            for metric_name, value in ue_data["measData"].items(): self.logger.debug(f"{log_prefix} ---Metric: {metric_name}, Value: {value}")
                else: self.logger.debug(f"{log_prefix} --No 'ueMeasData' in Style {kpm_report_style}.")


        # --- Aggregation Logic (currently for KPM Style 1 only) ---
        if kpm_report_style == 1:
            collet_start_time_dt = indication_hdr_extracted.get('colletStartTime')
            if collet_start_time_dt and isinstance(collet_start_time_dt, datetime):
                node_qos_class = self.e2_node_custom_configs.get(e2_agent_id, {}).get('qos')
                
                # Process metrics from the indication
                # KPM Style 1 metrics are in meas_data["measData"] = {'MetricName': [value]}
                reported_metrics_data = meas_data.get("measData", {})

                current_dl_thp = 0.0
                current_ul_thp = 0.0

                # Throughput Summing
                if self.THROUGHPUT_METRICS_FOR_SUMMING.issubset(set(self.all_subscribed_metrics)):
                    dl_values = reported_metrics_data.get("DRB.UEThpDl")
                    if dl_values and isinstance(dl_values, list) and len(dl_values) > 0:
                        try: current_dl_thp = float(dl_values[0])
                        except (ValueError, TypeError): self.logger.warning(f"THP Agg: Could not convert DRB.UEThpDl '{dl_values[0]}' for {e2_agent_id}")
                    
                    ul_values = reported_metrics_data.get("DRB.UEThpUl")
                    if ul_values and isinstance(ul_values, list) and len(ul_values) > 0:
                        try: current_ul_thp = float(ul_values[0])
                        except (ValueError, TypeError): self.logger.warning(f"THP Agg: Could not convert DRB.UEThpUl '{ul_values[0]}' for {e2_agent_id}")
                
                # Statistical Metrics Collection
                collected_stat_values = {} # {metric_name: value_float}
                for stat_metric_name in self.STATISTICAL_METRICS:
                    if stat_metric_name in self.all_subscribed_metrics: # Only process if subscribed
                        metric_val_list = reported_metrics_data.get(stat_metric_name)
                        if metric_val_list and isinstance(metric_val_list, list) and len(metric_val_list) > 0:
                            try:
                                collected_stat_values[stat_metric_name] = float(metric_val_list[0])
                            except (ValueError, TypeError):
                                self.logger.warning(f"STAT Agg: Could not convert {stat_metric_name} value '{metric_val_list[0]}' for {e2_agent_id}")
                        # else:
                            # self.logger.debug(f"STAT Agg: Metric {stat_metric_name} not found or empty in report from {e2_agent_id} for {collet_start_time_dt}")


                # Update aggregation window (thread-safe)
                with self.aggregation_lock:
                    agg_window = self.aggregation_windows[collet_start_time_dt]
                    
                    # Update throughput
                    agg_window["total_dl_throughput"] += current_dl_thp
                    agg_window["total_ul_throughput"] += current_ul_thp
                    if node_qos_class is not None:
                        agg_window["qos_dl_throughput"][node_qos_class] += current_dl_thp
                        agg_window["qos_ul_throughput"][node_qos_class] += current_ul_thp
                    
                    # Update statistical metric values
                    if node_qos_class is not None:
                        for metric_name, value in collected_stat_values.items():
                            agg_window["qos_metric_values"][node_qos_class][metric_name].append(value)
                            
                    agg_window["reported_nodes"].add(e2_agent_id)

                    # Check if window is complete
                    if len(self.subscribed_e2_node_ids) > 0 and \
                       len(agg_window["reported_nodes"]) == len(self.subscribed_e2_node_ids):
                        
                        # --- Log and Print Aggregated Results ---
                        ts_str = collet_start_time_dt.strftime('%Y-%m-%d %H:%M:%S')
                        self.logger.info(f"--- AGGREGATED TOTALS for ColletStartTime: {ts_str} ---")
                        self.logger.info(f"  Overall Total DRB.UEThpDl: {agg_window['total_dl_throughput']:.2f}")
                        self.logger.info(f"  Overall Total DRB.UEThpUl: {agg_window['total_ul_throughput']:.2f}")
                        
                        self.logger.info("  --- Per QoS Class Throughput ---")
                        q_thp_keys = sorted(list(set(agg_window["qos_dl_throughput"].keys()) | set(agg_window["qos_ul_throughput"].keys())))
                        for qos in q_thp_keys:
                            qos_dl = agg_window["qos_dl_throughput"].get(qos, 0.0)
                            qos_ul = agg_window["qos_ul_throughput"].get(qos, 0.0)
                            self.logger.info(f"    QoS Class {qos}: Thp_DL={qos_dl:.2f}, Thp_UL={qos_ul:.2f}")

                        self.logger.info("  --- Per QoS Class Statistical Metrics ---")
                        for qos, metrics_for_qos in sorted(agg_window["qos_metric_values"].items()):
                            self.logger.info(f"    QoS Class {qos}:")
                            for metric_name, values in sorted(metrics_for_qos.items()):
                                if values:
                                    val_min = min(values)
                                    val_max = max(values)
                                    val_avg = sum(values) / len(values)
                                    val_stdev = calculate_stdev(values)
                                    self.logger.info(f"      {metric_name}: Min={val_min:.2f}, Max={val_max:.2f}, Avg={val_avg:.2f}, StDev={val_stdev:.2f} (N={len(values)})")
                                else:
                                    self.logger.info(f"      {metric_name}: No data reported for this window/QoS.")
                        
                        self.logger.info(f"  Reported from DUs: {sorted(list(agg_window['reported_nodes']))}")
                        self.logger.info("-----------------------------------------------------")
                        
                        # Optionally print to stdout as well (can be verbose)
                        print(f"\n--- AGGREGATED TOTALS for ColletStartTime: {ts_str} ---")
                        print(f"  Overall Thp DL: {agg_window['total_dl_throughput']:.2f}, UL: {agg_window['total_ul_throughput']:.2f}")
                        # ... (condensed print for brevity, or replicate logger output)

                        del self.aggregation_windows[collet_start_time_dt] # Clean up
            
            elif kpm_report_style == 1: # collet_start_time_dt was invalid
                self.logger.warning(f"Aggregation: colletStartTime missing or not datetime for DU {e2_agent_id}.")


    def signal_handler(self, signum, frame):
        self.logger.info(f"Signal {signum} received, exiting xApp...")
        for handler in self.logger.handlers[:]:
            handler.close()
            self.logger.removeHandler(handler)
        super().signal_handler(signum, frame)

    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names_from_arg):
        report_period = 1000
        granul_period = 1000

        self.all_subscribed_metrics = list(metric_names_from_arg) # Store all metrics being subscribed
        for node_config in e2_node_configurations:
            self.subscribed_e2_node_ids.add(node_config['id'])
            self.e2_node_custom_configs[node_config['id']] = {
                'qos': node_config['qos'],
                'cpus': node_config['cpus']
            }

        self.logger.info(f"xApp will attempt aggregation for {len(self.subscribed_e2_node_ids)} DUs: {self.subscribed_e2_node_ids}")
        # Check for throughput metrics
        if not self.THROUGHPUT_METRICS_FOR_SUMMING.issubset(set(self.all_subscribed_metrics)):
            self.logger.warning(f"Not all target throughput metrics ({self.THROUGHPUT_METRICS_FOR_SUMMING}) are in the subscribed list. Throughput sum may be incomplete.")
        # Check for statistical metrics
        for stat_metric in self.STATISTICAL_METRICS:
            if stat_metric not in self.all_subscribed_metrics:
                self.logger.warning(f"Statistical metric '{stat_metric}' is not in the subscribed list. Stats for it will not be calculated.")
        
        if "DRB.AirIfDelayDist" in self.all_subscribed_metrics:
            self.logger.info("DRB.AirIfDelayDist is subscribed, raw values logged at DEBUG if enabled.")


        for node_config in e2_node_configurations:
            e2_node_id = node_config['id']
            # ... (subscription loop - no changes here from your provided code, uses metric_names_from_arg) ...
            qos_class = self.e2_node_custom_configs[e2_node_id]['qos']
            cpu_allocation = self.e2_node_custom_configs[e2_node_id]['cpus']
            self.logger.info(f"Processing subscriptions for E2 Node ID: {e2_node_id}, QoS: {qos_class}, CPUs: {cpu_allocation}")

            current_ue_id_for_style2 = ue_ids_config[0] if ue_ids_config else None
            # Pass the full list of metrics (metric_names_from_arg) to the subscription calls
            subscription_callback = lambda agent, sub, hdr, msg, ue_id_bound=current_ue_id_for_style2: \
                self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, ue_id_bound if kpm_report_style == 2 else None)

            if kpm_report_style == 1:
                self.logger.info(f"Subscribe to E2 node ID: {e2_node_id}, Style 1, metrics: {self.all_subscribed_metrics}")
                self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, self.all_subscribed_metrics, granul_period, subscription_callback)
            elif kpm_report_style == 2:
                if not ue_ids_config:
                    self.logger.error(f"Style 2 no UE ID for {e2_node_id}.")
                    continue
                # For Style 2, typically a subset of metrics or even one specific metric related to the UE condition is subscribed.
                # We pass all_subscribed_metrics, but the E2 Node/SM might only report on what's applicable for Style 2.
                self.logger.info(f"Subscribe to E2 node ID: {e2_node_id}, Style 2, UE: {current_ue_id_for_style2}, metrics: {self.all_subscribed_metrics}")
                self.e2sm_kpm.subscribe_report_service_style_2(e2_node_id, report_period, current_ue_id_for_style2, self.all_subscribed_metrics, granul_period, subscription_callback)
            # ... Add other styles 3,4,5 similarly, passing self.all_subscribed_metrics
            else:
                self.logger.info(f"Subscription for Style {kpm_report_style} not fully implemented for {e2_node_id}")



if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
    # ... (argparse setup - no changes from your provided code, except default metrics) ...
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
    parser.add_argument("--metrics", type=str, 
                        default='DRB.UEThpDl,DRB.UEThpUl,DRB.AirIfDelayDist,DRB.RlcSduDelayDl,DRB.AirIfDelayUl,DRB.RlcDelayUl,DRB.RlcPacketDropRateDl', 
                        help="Comma-separated list of Metrics names")
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level")


    args = parser.parse_args()
    # ... (arg parsing and validation - no changes from your provided code) ...
    log_level_int = getattr(logging, args.log_level.upper(), logging.INFO)
    if args.tdp_min_watts > args.tdp_max_watts: print(f"Error: TDP min > TDP max."); sys.exit(1)
    if args.tdp_min_watts < 0 or args.tdp_max_watts < 0: print(f"Error: TDP limits must be non-negative."); sys.exit(1)
    e2_node_ids_list_str = [n.strip() for n in args.e2_node_ids.split(",") if n.strip()]
    if not (len(e2_node_ids_list_str) == len(args.qos_classes) == len(args.cpu_allocations)):
        print("Error: Mismatch in count of e2_node_ids, qos_classes, cpu_allocations."); sys.exit(1)
    e2_node_configurations = []
    for i in range(len(e2_node_ids_list_str)):
        node_id, qos, cpu_str = e2_node_ids_list_str[i], args.qos_classes[i], args.cpu_allocations[i]
        if not (1 <= qos <= 4): print(f"Error: Invalid QoS {qos} for {node_id}."); sys.exit(1)
        parsed_cpus = parse_cpu_allocation(cpu_str)
        if parsed_cpus is None: print(f"Error: Invalid CPU alloc '{cpu_str}' for {node_id}."); sys.exit(1)
        e2_node_configurations.append({'id': node_id, 'qos': qos, 'cpus': parsed_cpus})
    if not e2_node_configurations: print("Error: No E2 Node configs."); sys.exit(1)
    
    metrics_list_from_arg = [m.strip() for m in args.metrics.split(",") if m.strip()]
    # Warnings about missing metrics
    if args.kpm_report_style == 1:
        if not {"DRB.UEThpDl", "DRB.UEThpUl"}.issubset(set(metrics_list_from_arg)):
            print("Warning: For throughput sum, 'DRB.UEThpDl' & 'DRB.UEThpUl' should be in --metrics for Style 1.")
        for sm in {"DRB.RlcSduDelayDl", "DRB.AirIfDelayUl", "DRB.RlcDelayUl", "DRB.RlcPacketDropRateDl"}:
            if sm not in metrics_list_from_arg:
                print(f"Warning: Statistical metric '{sm}' not in --metrics. Stats for it won't be calculated for Style 1.")


    myXapp = MyXapp(args.config, args.http_server_port, args.rmr_port, args.tdp_min_watts, args.tdp_max_watts, log_level_int)
    myXapp.e2sm_kpm.set_ran_func_id(args.ran_func_id)

    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with E2 Node configurations:") # Simplified startup print
    for cfg in e2_node_configurations: print(f"  - ID: {cfg['id']}, QoS: {cfg['qos']}, CPUs: {cfg['cpus']}")
    print(f"Global KPM Style: {args.kpm_report_style}, All Subscribed Metrics: {metrics_list_from_arg}")
    
    myXapp.start(e2_node_configurations, args.kpm_report_style, list(map(int, args.ue_ids.split(","))) if args.ue_ids else [], metrics_list_from_arg)
