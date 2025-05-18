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
import statistics

# Helper function to parse CPU allocation strings
def parse_cpu_allocation(cpu_str):
    # ... (no changes)
    if not cpu_str: return []
    cpus = set()
    parts = cpu_str.split(',')
    for part in parts:
        part = part.strip()
        if not part: continue
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
    if len(data_list) < 2: return 0.0
    try: return statistics.stdev(data_list)
    except statistics.StatisticsError: return 0.0

class MyXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port, tdp_min_watts, tdp_max_watts, prb_total, log_level=logging.INFO): # Added prb_total
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)

        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file_path = f"/mnt/data/xapp/xapp_{self.timestamp}.txt"
        self.logger = logging.getLogger(f"MyXapp_{self.timestamp}")
        # ... (logger setup - no changes) ...
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
        self.prb_total = prb_total # Store PrbTotal
        self.logger.info(f"Desired TDP Limit Range: {self.tdp_min_watts}W - {self.tdp_max_watts}W")
        if self.prb_total > 0:
            self.logger.info(f"Total PRBs per DU (for utilization %): {self.prb_total}")
        else:
            self.logger.warning(f"PrbTotal is {self.prb_total}. PRB Utilization percentage will not be calculated.")
        self.e2_node_custom_configs = {}

        self.aggregation_windows = defaultdict(lambda: {
            "total_dl_throughput": 0.0,
            "total_ul_throughput": 0.0,
            "total_dl_volume": 0.0, # New
            "total_ul_volume": 0.0, # New
            "reported_nodes": set(),
            "qos_dl_throughput": defaultdict(float),
            "qos_ul_throughput": defaultdict(float),
            "qos_dl_volume": defaultdict(float), # New
            "qos_ul_volume": defaultdict(float), # New
            "qos_metric_values": defaultdict(lambda: defaultdict(list))
        })
        self.subscribed_e2_node_ids = set()
        self.aggregation_lock = threading.Lock()
        self.all_subscribed_metrics = []
        self.THROUGHPUT_METRICS_FOR_SUMMING = {"DRB.UEThpDl", "DRB.UEThpUl"}
        self.VOLUME_METRICS_FOR_SUMMING = {"DRB.RlcSduTransmittedVolumeDL", "DRB.RlcSduTransmittedVolumeUL"} # New
        self.STATISTICAL_METRICS = {
            "DRB.RlcSduDelayDl",
            "DRB.AirIfDelayUl",
            "DRB.RlcDelayUl",
            "DRB.RlcPacketDropRateDl",
            "RRU.PrbUsedDl", # New
            "RRU.PrbUsedUl"  # New
        }

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id):
        try:
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            # ... (error logging - no changes) ...
            self.logger.error(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}", exc_info=True)
            self.logger.debug(f"Failed Header for {e2_agent_id}: {indication_hdr}")
            self.logger.debug(f"Failed Message for {e2_agent_id}: {indication_msg}")
            return

        # --- DEBUG Logging of all received metrics ---
        if self.logger.isEnabledFor(logging.DEBUG):
            # ... (comprehensive debug logging - no changes) ...
            log_prefix = f"[DU: {e2_agent_id}] [SubID: {subscription_id}]"
            self.logger.debug(f"{log_prefix} E2SM_KPM RIC Indication Content:") # ... and so on
            collet_start_time_log = indication_hdr_extracted.get('colletStartTime', 'N/A')
            self.logger.debug(f"{log_prefix} -ColletStartTime: {collet_start_time_log}")
            # ... (rest of your debug logging for different styles)


        # --- Aggregation Logic (currently for KPM Style 1 only) ---
        if kpm_report_style == 1:
            collet_start_time_dt = indication_hdr_extracted.get('colletStartTime')
            if collet_start_time_dt and isinstance(collet_start_time_dt, datetime):
                node_qos_class = self.e2_node_custom_configs.get(e2_agent_id, {}).get('qos')
                reported_metrics_data = meas_data.get("measData", {})
                
                current_dl_thp = 0.0
                current_ul_thp = 0.0
                current_dl_vol = 0.0
                current_ul_vol = 0.0

                # Throughput Summing
                if self.THROUGHPUT_METRICS_FOR_SUMMING.issubset(set(self.all_subscribed_metrics)):
                    dl_thp_val_list = reported_metrics_data.get("DRB.UEThpDl")
                    if dl_thp_val_list and isinstance(dl_thp_val_list, list) and len(dl_thp_val_list) > 0 and dl_thp_val_list[0] is not None:
                        try: current_dl_thp = float(dl_thp_val_list[0])
                        except (ValueError, TypeError): self.logger.debug(f"THP Agg: Could not convert DRB.UEThpDl '{dl_thp_val_list[0]}' for {e2_agent_id}")
                    ul_thp_val_list = reported_metrics_data.get("DRB.UEThpUl")
                    if ul_thp_val_list and isinstance(ul_thp_val_list, list) and len(ul_thp_val_list) > 0 and ul_thp_val_list[0] is not None:
                        try: current_ul_thp = float(ul_thp_val_list[0])
                        except (ValueError, TypeError): self.logger.debug(f"THP Agg: Could not convert DRB.UEThpUl '{ul_thp_val_list[0]}' for {e2_agent_id}")

                # Volume Summing
                if self.VOLUME_METRICS_FOR_SUMMING.issubset(set(self.all_subscribed_metrics)):
                    dl_vol_val_list = reported_metrics_data.get("DRB.RlcSduTransmittedVolumeDL")
                    if dl_vol_val_list and isinstance(dl_vol_val_list, list) and len(dl_vol_val_list) > 0 and dl_vol_val_list[0] is not None:
                        try: current_dl_vol = float(dl_vol_val_list[0])
                        except (ValueError, TypeError): self.logger.debug(f"VOL Agg: Could not convert DRB.RlcSduTransmittedVolumeDL '{dl_vol_val_list[0]}' for {e2_agent_id}")
                    ul_vol_val_list = reported_metrics_data.get("DRB.RlcSduTransmittedVolumeUL")
                    if ul_vol_val_list and isinstance(ul_vol_val_list, list) and len(ul_vol_val_list) > 0 and ul_vol_val_list[0] is not None:
                        try: current_ul_vol = float(ul_vol_val_list[0])
                        except (ValueError, TypeError): self.logger.debug(f"VOL Agg: Could not convert DRB.RlcSduTransmittedVolumeUL '{ul_vol_val_list[0]}' for {e2_agent_id}")

                # Statistical Metrics Collection
                collected_stat_values = {}
                for stat_metric_name in self.STATISTICAL_METRICS:
                    if stat_metric_name in self.all_subscribed_metrics:
                        metric_val_list = reported_metrics_data.get(stat_metric_name)
                        if metric_val_list and isinstance(metric_val_list, list) and len(metric_val_list) > 0:
                            metric_raw_value = metric_val_list[0]
                            if metric_raw_value is not None:
                                try: collected_stat_values[stat_metric_name] = float(metric_raw_value)
                                except (ValueError, TypeError): self.logger.debug(f"STAT Agg: Could not convert {stat_metric_name} value '{metric_raw_value}' for {e2_agent_id}")
                            else: self.logger.debug(f"STAT Agg: Metric {stat_metric_name} value is None for {e2_agent_id}. Skipping.")
                
                # Update aggregation window
                with self.aggregation_lock:
                    agg_window = self.aggregation_windows[collet_start_time_dt]
                    agg_window["total_dl_throughput"] += current_dl_thp
                    agg_window["total_ul_throughput"] += current_ul_thp
                    agg_window["total_dl_volume"] += current_dl_vol
                    agg_window["total_ul_volume"] += current_ul_vol
                    
                    if node_qos_class is not None:
                        agg_window["qos_dl_throughput"][node_qos_class] += current_dl_thp
                        agg_window["qos_ul_throughput"][node_qos_class] += current_ul_thp
                        agg_window["qos_dl_volume"][node_qos_class] += current_dl_vol
                        agg_window["qos_ul_volume"][node_qos_class] += current_ul_vol
                        for metric_name, value in collected_stat_values.items():
                            agg_window["qos_metric_values"][node_qos_class][metric_name].append(value)
                            
                    agg_window["reported_nodes"].add(e2_agent_id)

                    if len(self.subscribed_e2_node_ids) > 0 and \
                       len(agg_window["reported_nodes"]) == len(self.subscribed_e2_node_ids):
                        
                        ts_str = collet_start_time_dt.strftime('%Y-%m-%d %H:%M:%S')
                        self.logger.info(f"--- AGGREGATED TOTALS for ColletStartTime: {ts_str} ---")
                        self.logger.info(f"  Overall Total DRB.UEThpDl: {agg_window['total_dl_throughput']:.2f}, DRB.UEThpUl: {agg_window['total_ul_throughput']:.2f}")
                        self.logger.info(f"  Overall Total DRB.RlcSduTransmittedVolumeDL: {agg_window['total_dl_volume']:.0f}, DRB.RlcSduTransmittedVolumeUL: {agg_window['total_ul_volume']:.0f}")
                        
                        self.logger.info("  --- Per QoS Class Throughput & Volume ---")
                        q_sum_keys = sorted(list(set(agg_window["qos_dl_throughput"].keys()) | \
                                                 set(agg_window["qos_ul_throughput"].keys()) | \
                                                 set(agg_window["qos_dl_volume"].keys()) | \
                                                 set(agg_window["qos_ul_volume"].keys())))
                        for qos in q_sum_keys:
                            qos_dl_t = agg_window["qos_dl_throughput"].get(qos, 0.0)
                            qos_ul_t = agg_window["qos_ul_throughput"].get(qos, 0.0)
                            qos_dl_v = agg_window["qos_dl_volume"].get(qos, 0.0)
                            qos_ul_v = agg_window["qos_ul_volume"].get(qos, 0.0)
                            self.logger.info(f"    QoS Class {qos}: Thp(DL={qos_dl_t:.2f}, UL={qos_ul_t:.2f}), Vol(DL={qos_dl_v:.0f}, UL={qos_ul_v:.0f})")

                        self.logger.info("  --- Per QoS Class Statistical Metrics ---")
                        for qos, metrics_for_qos in sorted(agg_window["qos_metric_values"].items()):
                            self.logger.info(f"    QoS Class {qos}:")
                            for metric_name, values in sorted(metrics_for_qos.items()):
                                if values:
                                    val_min, val_max, val_avg, val_stdev = min(values), max(values), sum(values)/len(values), calculate_stdev(values)
                                    self.logger.info(f"      {metric_name}: Min={val_min:.2f}, Max={val_max:.2f}, Avg={val_avg:.2f}, StDev={val_stdev:.2f} (N={len(values)})")
                                    
                                    # PRB Utilization specific calculation
                                    if self.prb_total > 0 and metric_name in {"RRU.PrbUsedDl", "RRU.PrbUsedUl"}:
                                        util_values = [(v / self.prb_total) * 100 for v in values if v is not None]
                                        if util_values:
                                            util_min, util_max, util_avg, util_stdev = min(util_values), max(util_values), sum(util_values)/len(util_values), calculate_stdev(util_values)
                                            self.logger.info(f"        Utilization (%): Min={util_min:.2f}, Max={util_max:.2f}, Avg={util_avg:.2f}, StDev={util_stdev:.2f}")
                                else:
                                    self.logger.info(f"      {metric_name}: No valid data reported for this window/QoS.")
                        
                        self.logger.info(f"  Reported from DUs: {sorted(list(agg_window['reported_nodes']))}")
                        self.logger.info("-----------------------------------------------------")
                        
                        # Optional print to stdout
                        print(f"\n--- AGGREGATED TOTALS for ColletStartTime: {ts_str} ---")
                        print(f"  Overall Thp DL: {agg_window['total_dl_throughput']:.2f}, UL: {agg_window['total_ul_throughput']:.2f}")
                        print(f"  Overall Vol DL: {agg_window['total_dl_volume']:.0f}, UL: {agg_window['total_ul_volume']:.0f}")
                        
                        del self.aggregation_windows[collet_start_time_dt]
            
            elif kpm_report_style == 1:
                self.logger.warning(f"Aggregation: colletStartTime missing or not datetime for DU {e2_agent_id}.")

    # ... (signal_handler - no changes) ...
    def signal_handler(self, signum, frame):
        self.logger.info(f"Signal {signum} received, exiting xApp...")
        for handler in self.logger.handlers[:]: # Iterate over a copy
            handler.close()
            self.logger.removeHandler(handler)
        super().signal_handler(signum, frame) # Call parent's handler for graceful shutdown

    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names_from_arg):
        # ... (start method setup - no changes, just ensure all_subscribed_metrics is used in warnings) ...
        report_period = 1000
        granul_period = 1000

        self.all_subscribed_metrics = list(metric_names_from_arg) 
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
        # Check for volume metrics
        if not self.VOLUME_METRICS_FOR_SUMMING.issubset(set(self.all_subscribed_metrics)):
            self.logger.warning(f"Not all target volume metrics ({self.VOLUME_METRICS_FOR_SUMMING}) are in the subscribed list. Volume sum may be incomplete.")
        # Check for statistical metrics
        for stat_metric in self.STATISTICAL_METRICS:
            if stat_metric not in self.all_subscribed_metrics:
                self.logger.warning(f"Statistical metric '{stat_metric}' is not in the subscribed list. Stats for it will not be calculated.")
        
        # ... (rest of the start method, subscription loop) ...
        for node_config in e2_node_configurations:
            e2_node_id = node_config['id']
            qos_class = self.e2_node_custom_configs[e2_node_id]['qos'] # Already populated
            cpu_allocation = self.e2_node_custom_configs[e2_node_id]['cpus'] # Already populated
            self.logger.info(f"Processing subscriptions for E2 Node ID: {e2_node_id}, QoS: {qos_class}, CPUs: {cpu_allocation}")

            current_ue_id_for_style2 = ue_ids_config[0] if ue_ids_config else None
            subscription_callback = lambda agent, sub, hdr, msg, ue_id_bound=current_ue_id_for_style2: \
                self.my_subscription_callback(agent, sub, hdr, msg, kpm_report_style, ue_id_bound if kpm_report_style == 2 else None)

            if kpm_report_style == 1:
                self.logger.info(f"Subscribe to E2 node ID: {e2_node_id}, Style 1, metrics: {self.all_subscribed_metrics}")
                self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, self.all_subscribed_metrics, granul_period, subscription_callback)
            # ... (other KPM styles if needed) ...
            else:
                self.logger.info(f"Subscription for Style {kpm_report_style} not fully implemented for {e2_node_id}")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
    # ... (common args - no changes) ...
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")
    parser.add_argument("--e2_node_ids", type=str, required=True, help="Comma-separated list of E2 Node IDs")
    parser.add_argument("--qos_classes", type=int, nargs='+', required=True, help="List of QoS classes (1-4) for each E2 Node")
    parser.add_argument("--cpu_allocations", type=str, nargs='+', required=True, help="List of CPU allocations for each E2 Node")
    parser.add_argument("--tdp_min_watts", type=float, required=True, help="Minimum desired TDP limit in Watts")
    parser.add_argument("--tdp_max_watts", type=float, required=True, help="Maximum desired TDP limit in Watts")
    parser.add_argument("--prb_total", type=int, required=True, help="Total available PRBs per DU for utilization calculation (e.g., 100, 273)") # New
    parser.add_argument("--ran_func_id", type=int, default=2, help="RAN function ID")
    parser.add_argument("--kpm_report_style", type=int, default=1, choices=range(1,6), help="KPM Report Style (1-5)")
    parser.add_argument("--ue_ids", type=str, default='', help="Comma-separated list of UE IDs")
    parser.add_argument("--metrics", type=str, 
                        default='DRB.UEThpDl,DRB.UEThpUl,'
                                'DRB.RlcSduDelayDl,DRB.AirIfDelayUl,DRB.RlcDelayUl,DRB.RlcPacketDropRateDl,'
                                'RRU.PrbUsedDl,RRU.PrbUsedUl,' # Added PRB metrics
                                'DRB.RlcSduTransmittedVolumeDL,DRB.RlcSduTransmittedVolumeUL', # Added Volume metrics
                        help="Comma-separated list of Metrics names")
    parser.add_argument("--log_level", type=str, default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level")

    args = parser.parse_args()
    # ... (arg validation - no changes except for prb_total) ...
    log_level_int = getattr(logging, args.log_level.upper(), logging.INFO)
    if args.tdp_min_watts > args.tdp_max_watts: print(f"Error: TDP min > TDP max."); sys.exit(1)
    if args.prb_total <= 0:
        print(f"Warning: --prb_total is {args.prb_total}. It should be a positive integer. PRB Utilization % will not be calculated.")
        # Allow to proceed but PRB util won't work. Or sys.exit(1) if strict.

    # ... (e2_node_configurations setup - no changes) ...
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
    # Warnings about missing metrics for Style 1
    if args.kpm_report_style == 1:
        # Check throughput metrics
        if not {"DRB.UEThpDl", "DRB.UEThpUl"}.issubset(set(metrics_list_from_arg)):
            print("Warning: For throughput sum, 'DRB.UEThpDl' & 'DRB.UEThpUl' should be in --metrics for Style 1.")
        # Check volume metrics
        if not {"DRB.RlcSduTransmittedVolumeDL", "DRB.RlcSduTransmittedVolumeUL"}.issubset(set(metrics_list_from_arg)):
            print("Warning: For volume sum, 'DRB.RlcSduTransmittedVolumeDL' & 'DRB.RlcSduTransmittedVolumeUL' should be in --metrics for Style 1.")
        # Check statistical metrics
        all_stat_metrics = {"DRB.RlcSduDelayDl", "DRB.AirIfDelayUl", "DRB.RlcDelayUl", "DRB.RlcPacketDropRateDl", "RRU.PrbUsedDl", "RRU.PrbUsedUl"}
        for sm in all_stat_metrics:
            if sm not in metrics_list_from_arg:
                print(f"Warning: Statistical metric '{sm}' not in --metrics. Stats for it won't be calculated for Style 1.")


    # Pass prb_total to constructor
    myXapp = MyXapp(args.config, args.http_server_port, args.rmr_port, 
                    args.tdp_min_watts, args.tdp_max_watts, args.prb_total, log_level_int)
    myXapp.e2sm_kpm.set_ran_func_id(args.ran_func_id)

    # ... (signal handling and xApp start - no changes) ...
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with E2 Node configurations:") 
    for cfg in e2_node_configurations: print(f"  - ID: {cfg['id']}, QoS: {cfg['qos']}, CPUs: {cfg['cpus']}")
    print(f"Total PRBs (for util %): {args.prb_total if args.prb_total > 0 else 'N/A'}")
    print(f"Global KPM Style: {args.kpm_report_style}, All Subscribed Metrics: {metrics_list_from_arg}")
    
    myXapp.start(e2_node_configurations, args.kpm_report_style, list(map(int, args.ue_ids.split(","))) if args.ue_ids else [], metrics_list_from_arg)
