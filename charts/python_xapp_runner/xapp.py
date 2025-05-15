#!/usr/bin/env python3
import sys
import argparse
import signal
from lib.xAppBase import xAppBase # Assuming this is your xApp base library
from datetime import datetime # For datetime.now(), and can be used for isinstance checks
# import datetime as dt # Alternative for clarity: dt.datetime
import re # For parsing CPU ranges
import threading # For the dummy periodic processor
import time      # For the dummy periodic processor
import queue     # For passing data to the dummy periodic processor

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
    def __init__(self, config, http_server_port, rmr_port, tdp_min_watts, tdp_max_watts):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)

        self.instance_timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self.log_file = f"/mnt/data/xapp/xapp_{self.instance_timestamp}.txt"
        print(f"Logging RIC Indication data to: {self.log_file}")

        self.tdp_min_watts = tdp_min_watts
        self.tdp_max_watts = tdp_max_watts
        print(f"Desired TDP Limit Range: {self.tdp_min_watts}W - {self.tdp_max_watts}W")
        self.e2_node_custom_configs = {}

        # For the _periodic_aggregation_processor
        # The user should integrate this with their actual data passing mechanism
        self.data_queue_for_aggregator = queue.Queue()
        self.aggregation_thread = None
        self.stop_aggregation_event = threading.Event()

    # --- THIS IS A PLACEHOLDER for the user's _periodic_aggregation_processor ---
    # The user needs to ADAPT or REPLACE this with their actual method.
    # The key is how 'collet_start_time_from_data' is handled.
    def _periodic_aggregation_processor(self):
        print(f"[{datetime.now()}] AGG_PROC_THREAD: Starting _periodic_aggregation_processor thread...")
        while not self.stop_aggregation_event.is_set():
            try:
                # This is an EXAMPLE of how data might be retrieved.
                # The user's actual data item structure passed to this processor might be different.
                data_item = self.data_queue_for_aggregator.get(timeout=1)
                if data_item is None: # Sentinel to stop the thread
                    print(f"[{datetime.now()}] AGG_PROC_THREAD: Received None, stopping.")
                    break

                # --- IMPORTANT PART ---
                # Assuming data_item is a dictionary that includes 'colletStartTime'
                # which was extracted by self.e2sm_kpm.extract_hdr_info()
                # and is ALREADY a datetime.datetime object or the string 'N/A'.
                collet_start_time_from_data = data_item.get('colletStartTime') # This is the crucial variable
                e2_node_id_from_data = data_item.get('e2_node_id', 'UnknownDU')
                # --- END OF IMPORTANT PART ---

                # print(f"[{datetime.now()}] AGG_PROC: Received item for DU {e2_node_id_from_data} with colletStartTime: {collet_start_time_from_data} (type: {type(collet_start_time_from_data)})")

                collet_start_dt_for_logic = None # Initialize the variable to hold the processed datetime

                # --- CORRECT HANDLING OF collet_start_time_from_data ---
                # This is where the fix for the TypeError happens.
                if isinstance(collet_start_time_from_data, datetime): # Check if it's a datetime.datetime object
                    collet_start_dt_for_logic = collet_start_time_from_data # Use it directly
                    # print(f"[{datetime.now()}] AGG_PROC: DU {e2_node_id_from_data}: colletStartTime is datetime object: {collet_start_dt_for_logic}")
                    # Now you can use collet_start_dt_for_logic for comparisons, formatting, etc.
                    # Example: collet_start_dt_for_logic.strftime("%Y-%m-%d %H:%M:%S")

                elif isinstance(collet_start_time_from_data, str) and collet_start_time_from_data == 'N/A':
                    print(f"[{datetime.now()}] AGG_PROC: DU {e2_node_id_from_data}: colletStartTime is 'N/A'. May skip further processing.")
                    # collet_start_dt_for_logic remains None, or handle as an error

                elif isinstance(collet_start_time_from_data, str):
                    # This case should ideally not occur if extract_hdr_info works as expected
                    # and 'N/A' is the only string fallback from .get().
                    print(f"[{datetime.now()}] AGG_PROC: DU {e2_node_id_from_data}: colletStartTime is an unexpected string '{collet_start_time_from_data}'.")
                    # If you *did* expect other string formats, you would parse them here.
                    # However, the original error indicates 'collet_start_time_from_data' was a datetime object.
                    # The line that WOULD cause the error if collet_start_time_from_data was a datetime object:
                    #   collet_start_dt_for_logic = datetime.strptime(collet_start_time_from_data, "%Y-%m-%d %H:%M:%S")
                    pass # Or log an error, as this is unexpected.

                else:
                    # It's neither a datetime object nor a string we explicitly handle
                    print(f"[{datetime.now()}] AGG_PROC: DU {e2_node_id_from_data}: colletStartTime is of an unexpected type ({type(collet_start_time_from_data)}): {collet_start_time_from_data}")
                    # collet_start_dt_for_logic remains None

                # --- END OF CORRECT HANDLING ---

                if collet_start_dt_for_logic:
                    # ... THE USER'S ACTUAL AGGREGATION LOGIC using collet_start_dt_for_logic ...
                    # print(f"[{datetime.now()}] AGG_PROC: DU {e2_node_id_from_data}: Successfully processed item with collet_start_dt: {collet_start_dt_for_logic}")
                    pass # Placeholder for actual work
                else:
                    # print(f"[{datetime.now()}] AGG_PROC: DU {e2_node_id_from_data}: Failed to determine valid collet_start_dt for this item. Skipping.")
                    pass

                self.data_queue_for_aggregator.task_done()
            except queue.Empty:
                # Timeout is normal, allows checking stop_aggregation_event
                continue
            except Exception as e:
                print(f"[{datetime.now()}] AGG_PROC_THREAD: Error in _periodic_aggregation_processor loop: {e}")
                # import traceback # For more detailed debugging if needed
                # traceback.print_exc()
        print(f"[{datetime.now()}] AGG_PROC_THREAD: _periodic_aggregation_processor thread finished.")
    # --- END OF PLACEHOLDER ---

    def my_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id):
        current_time_str = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        if kpm_report_style == 2:
            print(f"\n[{current_time_str}] RIC Indication from {e2_agent_id} [SubID: {subscription_id}], Style: {kpm_report_style}, UE: {ue_id}")
        else:
            print(f"\n[{current_time_str}] RIC Indication from {e2_agent_id} [SubID: {subscription_id}], Style: {kpm_report_style}")

        try:
            # self.e2sm_kpm.extract_hdr_info MODIFIES indication_hdr IN PLACE or returns a new dict.
            # 'colletStartTime' in the returned dict will be a datetime.datetime object or original if not parsable.
            indication_hdr_extracted = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)
        except Exception as e:
            print(f"Error extracting KPM data from E2 node {e2_agent_id}: {e}")
            with open(self.log_file, "a", buffering=1) as file_obj:
                file_obj.write(f"[{datetime.now()}] ERROR processing indication from {e2_agent_id} for sub {subscription_id}: {e}\n")
                file_obj.write(f"Raw Header: {indication_hdr}\n")
                file_obj.write(f"Raw Message: {indication_msg}\n")
            return

        # 'colletStartTime' in indication_hdr_extracted is now a datetime.datetime object
        # (due to extract_hdr_info) or 'N/A' (if using .get with 'N/A' as default and key not found).
        collet_start_time_as_object = indication_hdr_extracted.get('colletStartTime', 'N/A')

        # Example: Pass relevant data to the aggregation processor via queue
        # The user needs to adapt this to how their _periodic_aggregation_processor actually gets data.
        if self.aggregation_thread and self.aggregation_thread.is_alive():
            item_for_aggregator = {
                'e2_node_id': e2_agent_id,
                'subscription_id': subscription_id,
                'colletStartTime': collet_start_time_as_object, # This is ALREADY datetime or 'N/A'
                'meas_data': meas_data, # Pass whatever part of data is needed
                'received_at': datetime.now() # For debugging/latency checks
            }
            self.data_queue_for_aggregator.put(item_for_aggregator)

        # Logging to file
        def redirect_output_to_file(output_str, file_obj_handle):
            file_obj_handle.write(output_str + "\n")
            file_obj_handle.flush()

        with open(self.log_file, "a", buffering=1) as file_obj:
            log_timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            log_prefix = f"[{log_timestamp}] [DU: {e2_agent_id}] [SubID: {subscription_id}]"

            redirect_output_to_file(f"{log_prefix} E2SM_KPM RIC Indication Content:", file_obj)
            # Displaying the collet_start_time_as_object. If it's datetime, it will print nicely.
            redirect_output_to_file(f"{log_prefix} -ColletStartTime: {collet_start_time_as_object} (type: {type(collet_start_time_as_object)})", file_obj)
            # Also print to console for immediate feedback
            print(f"  -ColletStartTime: {collet_start_time_as_object} (type: {type(collet_start_time_as_object)})")

            redirect_output_to_file(f"{log_prefix} -Measurements Data:", file_obj)
            granulPeriod = meas_data.get("granulPeriod", None)
            if granulPeriod is not None:
                redirect_output_to_file(f"{log_prefix} -granulPeriod: {granulPeriod}", file_obj)

            if kpm_report_style in [1, 2]:
                if "measData" in meas_data:
                    for metric_name, value in meas_data["measData"].items():
                        redirect_output_to_file(f"{log_prefix} --Metric: {metric_name}, Value: {value}", file_obj)
                else:
                    redirect_output_to_file(f"{log_prefix} --No 'measData' found in KPM Style {kpm_report_style} report.", file_obj)
            else: # Styles 3, 4, 5 involve ueMeasData
                if "ueMeasData" in meas_data:
                    for ue_id_report, ue_meas_data in meas_data["ueMeasData"].items():
                        redirect_output_to_file(f"{log_prefix} --UE_id: {ue_id_report}", file_obj)
                        granulPeriod_ue = ue_meas_data.get("granulPeriod", None)
                        if granulPeriod_ue is not None:
                            redirect_output_to_file(f"{log_prefix} ---granulPeriod: {granulPeriod_ue}", file_obj)
                        if "measData" in ue_meas_data:
                            for metric_name, value in ue_meas_data["measData"].items():
                                redirect_output_to_file(f"{log_prefix} ---Metric: {metric_name}, Value: {value}", file_obj)
                        else:
                            redirect_output_to_file(f"{log_prefix} ---No 'measData' found for UE {ue_id_report}.", file_obj)
                else:
                    redirect_output_to_file(f"{log_prefix} --No 'ueMeasData' found in KPM Style {kpm_report_style} report.", file_obj)

    @xAppBase.start_function
    def start(self, e2_node_configurations, kpm_report_style, ue_ids_config, metric_names):
        report_period = 1000
        granul_period = 1000

        # Start the aggregation thread (if not already running)
        if not self.aggregation_thread or not self.aggregation_thread.is_alive():
            self.stop_aggregation_event.clear() # Ensure it's not set from a previous run
            self.aggregation_thread = threading.Thread(target=self._periodic_aggregation_processor, daemon=True)
            self.aggregation_thread.start()
            print("Placeholder aggregation processor thread started.")

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
                current_ue_ids_for_style5 = list(ue_ids_config) # Make a copy
                if len(current_ue_ids_for_style5) < 2:
                    if not current_ue_ids_for_style5:
                        dummyUeId1, dummyUeId2 = 1, 2 # Arbitrary dummy IDs
                        current_ue_ids_for_style5.extend([dummyUeId1, dummyUeId2])
                        print(f"INFO: For E2 Node {e2_node_id}, Style 5 requires at least two UE IDs. Added dummy UeIDs: {dummyUeId1}, {dummyUeId2}")
                    else: # one UE ID present
                        dummyUeId = current_ue_ids_for_style5[0] + 1 # Create a second dummy
                        current_ue_ids_for_style5.append(dummyUeId)
                        print(f"INFO: For E2 Node {e2_node_id}, Style 5 requires at least two UE IDs. Added dummy UeID: {dummyUeId}")
                print(f"Subscribe to E2 node ID: {e2_node_id}, RAN func: e2sm_kpm, Report Style: 5, UE_ids: {current_ue_ids_for_style5}, metrics: {metric_names}")
                self.e2sm_kpm.subscribe_report_service_style_5(e2_node_id, report_period, current_ue_ids_for_style5, metric_names, granul_period, subscription_callback)
            else:
                print(f"INFO: Subscription for E2SM_KPM Report Service Style {kpm_report_style} is not supported for E2 node {e2_node_id}")

    # Override signal_handler to stop the aggregation thread gracefully
    def signal_handler(self, signum, frame):
        print(f"Signal {signum} received, xApp shutting down...")
        if self.aggregation_thread and self.aggregation_thread.is_alive():
            print("Attempting to stop aggregation processor thread...")
            self.stop_aggregation_event.set()
            try:
                # Put a sentinel value to unblock the queue.get if it's waiting indefinitely
                self.data_queue_for_aggregator.put(None, block=False)
            except queue.Full:
                print("Warning: Aggregation queue is full, cannot add sentinel. Thread might take longer to stop.")
                pass # Thread will stop on next timeout or stop_event check
            
            self.aggregation_thread.join(timeout=5) # Wait up to 5 seconds
            if self.aggregation_thread.is_alive():
                print("Warning: Aggregation thread did not stop in time.")
            else:
                print("Aggregation processor thread stopped.")
        else:
            print("Aggregation processor thread was not running or already stopped.")
        
        super().signal_handler(signum, frame) # Call base class handler for RMR cleanup etc.
        print("xApp shutdown process complete.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
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
    parser.add_argument("--metrics", type=str, default='DRB.UEThpUl,DRB.UEThpDl', help="Comma-separated list of Metrics names (e.g., 'Metric1,Metric2')")

    args = parser.parse_args()

    if args.tdp_min_watts > args.tdp_max_watts:
        print(f"Error: TDP min watts ({args.tdp_min_watts}) cannot be greater than TDP max watts ({args.tdp_max_watts}).")
        sys.exit(1)
    if args.tdp_min_watts < 0 or args.tdp_max_watts < 0:
        print(f"Error: TDP limits must be non-negative.")
        sys.exit(1)

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
        if parsed_cpus is None: # parse_cpu_allocation returns None on error
            print(f"Error: Invalid CPU allocation format '{cpu_str}' for E2 Node '{node_id}'. Exiting.")
            sys.exit(1)
        e2_node_configurations.append({'id': node_id, 'qos': qos, 'cpus': parsed_cpus})

    if not e2_node_configurations:
        print("Error: No E2 Node configurations provided or parsed successfully.")
        sys.exit(1)

    config = args.config
    ran_func_id = args.ran_func_id
    ue_ids_list = list(map(int, args.ue_ids.split(","))) if args.ue_ids.strip() else []
    kpm_report_style = args.kpm_report_style
    metrics_list = [metric.strip() for metric in args.metrics.split(",") if metric.strip()]

    myXapp = MyXapp(config, args.http_server_port, args.rmr_port, args.tdp_min_watts, args.tdp_max_watts)
    # Assuming self.e2sm_kpm is initialized by xAppBase or its superclass.
    # If not, you would need to do: from e2sm_kpm_module import e2sm_kpm_module
    # and then in MyXapp.__init__: self.e2sm_kpm = e2sm_kpm_module(self)
    # Your provided e2sm_kpm_module suggests it's an attribute of the xApp.
    if not hasattr(myXapp, 'e2sm_kpm'):
        print("CRITICAL: myXapp.e2sm_kpm is not initialized. Please ensure e2sm_kpm_module is correctly integrated.")
        # from e2sm_kpm_module import e2sm_kpm_module # You would need this file in the path
        # myXapp.e2sm_kpm = e2sm_kpm_module(myXapp)
        # For now, assuming it's there from the base class or similar mechanism.
        # If the script fails here, this is the likely cause.
        # sys.exit(1) # This would be safer if you know it should be there
        pass # Proceeding with caution

    if hasattr(myXapp, 'e2sm_kpm') and myXapp.e2sm_kpm is not None:
         myXapp.e2sm_kpm.set_ran_func_id(ran_func_id)
    else:
        print("Warning: e2sm_kpm module not available on myXapp object, cannot set RAN func ID.")


    # Connect exit signals. The overridden signal_handler in MyXapp will be called.
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    print(f"\nStarting xApp with the following E2 Node configurations:")
    for cfg_node in e2_node_configurations:
        print(f"  - ID: {cfg_node['id']}, QoS: {cfg_node['qos']}, CPUs: {cfg_node['cpus']}")
    print(f"Global KPM Style: {kpm_report_style}, Metrics: {metrics_list}, UE IDs (for relevant styles): {ue_ids_list if ue_ids_list else 'N/A'}")
    
    try:
        myXapp.start(e2_node_configurations, kpm_report_style, ue_ids_list, metrics_list)
        # The start method is decorated with @xAppBase.start_function,
        # which likely means it starts a blocking loop.
        # If it's non-blocking, you might need a time.sleep() loop here to keep main alive.
        # Based on typical xAppBase design, it's probably blocking.
        while not myXapp.is_terminated(): # Assuming xAppBase has an is_terminated method
            time.sleep(1) # Keep main thread alive while xApp runs

    except KeyboardInterrupt:
        print("\nKeyboard interrupt received in main, initiating shutdown...")
        if not myXapp.is_terminated():
            myXapp.signal_handler(signal.SIGINT, None) # Manually trigger shutdown sequence
    except Exception as e_main:
        print(f"Main execution error: {e_main}")
        import traceback
        traceback.print_exc()
        if not myXapp.is_terminated():
            myXapp.signal_handler(signal.SIGTERM, None) # Attempt graceful shutdown
    finally:
        print("xApp main execution loop finished or xApp terminated.")
        # Ensure aggregation thread is stopped if somehow still running
        if myXapp.aggregation_thread and myXapp.aggregation_thread.is_alive():
            print("Final check: ensuring aggregation thread is stopped.")
            myXapp.stop_aggregation_event.set()
            try: myXapp.data_queue_for_aggregator.put(None, block=False)
            except queue.Full: pass
            myXapp.aggregation_thread.join(timeout=2)
