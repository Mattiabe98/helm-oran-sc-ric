#!/usr/bin/env python3

"""
Server to receive metrics data from a running srsRAN Project gnb via UDP
and from a Near-RT RIC via E2 interface, and send all metrics to InfluxDB.
"""

import argparse
import json
import logging
import signal
import socket
from contextlib import suppress
from datetime import datetime, timezone
from http.client import RemoteDisconnected
from queue import Queue
from threading import Thread
from time import sleep
from typing import Any, Dict, Optional, Tuple, List

from influxdb_client import InfluxDBClient, WriteApi
from influxdb_client.client.write_api import SYNCHRONOUS

# --- Dependency for App 2 ---
# Assume 'lib.xAppBase' is in the Python path or a local 'lib' directory
try:
    from lib.xAppBase import xAppBase
except ImportError:
    logging.error("Could not import xAppBase. Make sure 'lib' directory is accessible.")
    # You might want to exit here or handle this differently
    # For now, define a dummy class to allow the script structure to work
    class xAppBase:
        start_function = lambda f: f # Dummy decorator
        def __init__(self, *args, **kwargs): pass
        def signal_handler(self, *args, **kwargs): pass
        class DummyE2SM:
            def set_ran_func_id(self, *args, **kwargs): pass
            def extract_hdr_info(self, hdr): return {'colletStartTime': datetime.now(timezone.utc).timestamp()} # Dummy data
            def extract_meas_data(self, msg): return {'measData': {'dummyMetric': 123.0}} # Dummy data
            def subscribe_report_service_style_1(self, *args, **kwargs): pass
            def subscribe_report_service_style_2(self, *args, **kwargs): pass
            def subscribe_report_service_style_3(self, *args, **kwargs): pass
            def subscribe_report_service_style_4(self, *args, **kwargs): pass
            def subscribe_report_service_style_5(self, *args, **kwargs): pass
        e2sm_kpm = DummyE2SM()
        logging.warning("Using dummy xAppBase class.")

# --- Global Queue for metrics ---
# Shared between UDP receiver, RIC callback, and InfluxDB pusher
metrics_queue: Queue = Queue()

# --- xApp Class Definition (Modified for Integration) ---
class IntegratedXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port, influx_bucket, influx_testbed):
        super(IntegratedXapp, self).__init__(config, http_server_port, rmr_port)
        self.influx_bucket = influx_bucket
        self.influx_testbed = influx_testbed
        logging.info("IntegratedXapp initialized.")

    def ric_subscription_callback(self, e2_agent_id, subscription_id, indication_hdr, indication_msg, kpm_report_style, ue_id_from_sub=None):
        """
        Callback triggered by xAppBase upon receiving RIC Indication.
        Formats data and puts it onto the shared metrics_queue.
        """
        try:
            log_prefix = f"RIC Indication from {e2_agent_id} (SubID: {subscription_id}, Style: {kpm_report_style}"
            if ue_id_from_sub is not None:
                log_prefix += f", UE: {ue_id_from_sub}"
            if kpm_report_style == 2 and ue_id_from_sub is None:
                 log_prefix += f", Orig UE: {ue_id_from_sub if ue_id_from_sub else 'N/A'}"
            log_prefix += ")"
            logging.info(log_prefix)

            hdr_info = self.e2sm_kpm.extract_hdr_info(indication_hdr)
            meas_data = self.e2sm_kpm.extract_meas_data(indication_msg)

            # Use colletStartTime if available, otherwise use current time
            timestamp_unix = hdr_info.get('colletStartTime', datetime.now(timezone.utc).timestamp())
            # Ensure timestamp is timezone-aware timezone.utc for InfluxDB
            timestamp_iso = datetime.fromtimestamp(timestamp_unix, tz=timezone.timezone.utc).isoformat()

            granulPeriod = meas_data.get("granulPeriod")

            common_tags = {
                "e2_agent_id": str(e2_agent_id),
                "subscription_id": str(subscription_id),
                "report_style": kpm_report_style,
                "testbed": self.influx_testbed,
            }
            if granulPeriod is not None:
                common_tags["granularity_ms"] = granulPeriod

            if kpm_report_style in [1, 2]:
                # Style 1: Cell level metrics
                # Style 2: Single UE metrics (UE ID passed during subscription)
                fields = convert_integers_to_floats(meas_data.get("measData", {}))
                if not fields:
                    logging.warning(f"{log_prefix}: No measurement data found in measData.")
                    return

                record = {
                    "source": "ric", # Identifier for the pusher thread
                    "measurement": f"ric_kpm_style{kpm_report_style}",
                    "tags": common_tags.copy(),
                    "fields": fields,
                    "time": timestamp_iso,
                }
                # Add UE ID tag specifically for style 2
                if kpm_report_style == 2 and ue_id_from_sub is not None:
                    record["tags"]["ue_id"] = str(ue_id_from_sub)

                metrics_queue.put(record)
                logging.debug(f"Queued RIC KPM Style {kpm_report_style} metric: {record}")

            elif kpm_report_style in [3, 4, 5]:
                # Style 3: Cell level metrics for matching UEs
                # Style 4: Per-UE metrics for matching UEs
                # Style 5: Per-UE metrics for specific UE list
                ue_meas_dict = meas_data.get("ueMeasData", {})
                if not ue_meas_dict:
                     logging.warning(f"{log_prefix}: No measurement data found in ueMeasData.")
                     return

                for ue_id, ue_meas_data in ue_meas_dict.items():
                    fields = convert_integers_to_floats(ue_meas_data.get("measData", {}))
                    if not fields:
                        logging.warning(f"{log_prefix}: No measurement data for UE {ue_id}.")
                        continue

                    # Granularity might be per-UE here
                    ueGranulPeriod = ue_meas_data.get("granulPeriod", granulPeriod)

                    record = {
                        "source": "ric", # Identifier for the pusher thread
                        "measurement": f"ric_kpm_style{kpm_report_style}_ue",
                        "tags": common_tags.copy(),
                        "fields": fields,
                        "time": timestamp_iso,
                    }
                    record["tags"]["ue_id"] = str(ue_id)
                    if ueGranulPeriod is not None:
                       record["tags"]["granularity_ms"] = ueGranulPeriod # Override common tag if specific

                    metrics_queue.put(record)
                    logging.debug(f"Queued RIC KPM Style {kpm_report_style} UE metric: {record}")
            else:
                logging.warning(f"{log_prefix}: Unsupported KPM report style {kpm_report_style} encountered.")

        except Exception as e:
            logging.exception(f"Error processing RIC indication: {e}")


    # Mark the function as xApp start function using xAppBase.start_function decorator.
    @xAppBase.start_function
    def start(self, e2_node_id, kpm_report_style, ue_ids: List[int], metric_names: List[str]):
        """Starts the xApp logic, primarily setting up subscriptions."""
        report_period = 1000  # ms - TODO: Make configurable?
        granul_period = 1000  # ms - TODO: Make configurable?
        logging.info(f"Setting up RIC subscription: Node={e2_node_id}, Style={kpm_report_style}, UEs={ue_ids}, Metrics={metric_names}")

        # Use always the same subscription callback, binding necessary style/UE info
        if kpm_report_style == 1:
            subscription_callback = lambda agent, sub, hdr, msg: self.ric_subscription_callback(agent, sub, hdr, msg, 1)
            logging.info(f"Subscribing Style 1: Node={e2_node_id}, Metrics={metric_names}")
            self.e2sm_kpm.subscribe_report_service_style_1(e2_node_id, report_period, metric_names, granul_period, subscription_callback)

        elif kpm_report_style == 2:
            # Bind the specific UE ID used for subscription
            ue_id_for_sub = ue_ids[0]
            subscription_callback = lambda agent, sub, hdr, msg: self.ric_subscription_callback(agent, sub, hdr, msg, 2, ue_id_from_sub=ue_id_for_sub)
            logging.info(f"Subscribing Style 2: Node={e2_node_id}, UE={ue_id_for_sub}, Metrics={metric_names}")
            self.e2sm_kpm.subscribe_report_service_style_2(e2_node_id, report_period, ue_id_for_sub, metric_names, granul_period, subscription_callback)

        elif kpm_report_style == 3:
             subscription_callback = lambda agent, sub, hdr, msg: self.ric_subscription_callback(agent, sub, hdr, msg, 3)
             if len(metric_names) > 1:
                 metric_names = metric_names[0]
                 logging.warning(f"Style 3 only supports 1 metric, using: {metric_names}")
             # TODO: Define actual matching conditions based on needs/arguments
             matchingConds = [{'matchingCondChoice': ('testCondInfo', {'testType': ('ul-rSRP', 'true'), 'testExpr': 'lessthan', 'testValue': ('valueInt', 1000)})}] # Example condition
             logging.info(f"Subscribing Style 3: Node={e2_node_id}, Metrics={metric_names}, Conditions={matchingConds}")
             self.e2sm_kpm.subscribe_report_service_style_3(e2_node_id, report_period, matchingConds, metric_names, granul_period, subscription_callback)

        elif kpm_report_style == 4:
            subscription_callback = lambda agent, sub, hdr, msg: self.ric_subscription_callback(agent, sub, hdr, msg, 4)
             # TODO: Define actual matching conditions based on needs/arguments
            matchingUeConds = [{'testCondInfo': {'testType': ('ul-rSRP', 'true'), 'testExpr': 'lessthan', 'testValue': ('valueInt', 1000)}}] # Example condition
            logging.info(f"Subscribing Style 4: Node={e2_node_id}, Metrics={metric_names}, Conditions={matchingUeConds}")
            self.e2sm_kpm.subscribe_report_service_style_4(e2_node_id, report_period, matchingUeConds, metric_names, granul_period, subscription_callback)

        elif kpm_report_style == 5:
            subscription_callback = lambda agent, sub, hdr, msg: self.ric_subscription_callback(agent, sub, hdr, msg, 5)
            if len(ue_ids) < 2:
                dummyUeId = ue_ids[0] + 1 if ue_ids else 999
                ue_ids.append(dummyUeId)
                logging.warning(f"Style 5 requires >= 2 UEs, adding dummy UE: {dummyUeId}")
            logging.info(f"Subscribing Style 5: Node={e2_node_id}, UEs={ue_ids}, Metrics={metric_names}")
            self.e2sm_kpm.subscribe_report_service_style_5(e2_node_id, report_period, ue_ids, metric_names, granul_period, subscription_callback)

        else:
            logging.error(f"Subscription for E2SM_KPM Report Service Style {kpm_report_style} is not supported.")
            # Consider raising an exception or exiting if this is critical
            # exit(1)

# --- srsRAN UDP Listener Functions (Mostly Unchanged) ---

def _start_metric_server(
    port: int,
    max_buffer_size: int = 1024**2,
) -> None:
    """Listens for UDP packets, parses JSON, and puts data onto the shared queue."""
    # Create Server
    server_socket = socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except AttributeError:
        pass  # Some systems don't support SO_REUSEPORT

    try:
        server_socket.bind(("0.0.0.0", port))
        logging.info(f"UDP server listening on port {port}")
    except OSError as e:
        logging.error(f"Failed to bind UDP socket to port {port}: {e}")
        # Signal the pusher thread to exit since we can't receive UDP
        metrics_queue.put(None)
        return # Exit thread

    text = ""
    while True:
        try:
            line = server_socket.recv(max_buffer_size).decode()
            if not line:
                # Indicates shutdown signal (empty packet sent in close())
                logging.info("UDP server received shutdown signal.")
                break
        except OSError as e:
            # This can happen if the socket is closed by another thread during recv
            logging.warning(f"UDP socket error: {e}. Shutting down listener.")
            break
        except Exception as e:
             logging.exception(f"Unexpected error receiving UDP data: {e}")
             continue # Try to recover

        text += line.strip()

        # Handle potentially multiple JSON objects concatenated without separators
        header = ""
        processed_items = 0
        try:
            # More robust split for adjacent JSONs: ...}{...
            parts = text.split('}{')
            if len(parts) == 1: # No split, might be partial or single JSON
                item_to_process = text
                text_remaining = ""
            else:
                # Process all but the last part, which might be incomplete
                items_to_process = []
                for i, part in enumerate(parts[:-1]):
                    items_to_process.append((header + part + "}").strip())
                    header = "{" # Prepend '{' for the next part
                # The last part needs the prepended '{' unless it was the only part
                text_remaining = (header + parts[-1]).strip() if len(parts)>1 else parts[-1].strip()

                for item_str in items_to_process:
                     try:
                         metric_dict = json.loads(item_str)
                         metric_dict["source"] = "srsran" # Add source identifier
                         metrics_queue.put(metric_dict)
                         processed_items += 1
                     except json.JSONDecodeError:
                         logging.error(f"Error decoding srsRAN JSON chunk: {item_str}")

                item_to_process = text_remaining # Try to process the remainder

            # Attempt to parse the (potentially remaining) text
            if item_to_process:
                 with suppress(json.JSONDecodeError):
                     metric_dict = json.loads(item_to_process)
                     metric_dict["source"] = "srsran" # Add source identifier
                     metrics_queue.put(metric_dict)
                     processed_items += 1
                     text_remaining = "" # Successfully parsed

            text = text_remaining # Keep only unparsed text for next iteration

            if processed_items > 0:
                logging.debug(f"Queued {processed_items} srsRAN metric(s) from UDP.")

        except Exception as e:
            logging.exception(f"Error processing received UDP data: {e}")
            text = "" # Clear buffer on major error


    logging.info("UDP metric server stopped.")
    server_socket.close()


# --- InfluxDB Pusher Function (Modified) ---

def _publish_data(
    client: InfluxDBClient,
    bucket: str,
    testbed: str,
) -> None:
    """Gets metrics from the queue and pushes them to InfluxDB."""
    try:
        write_api = client.write_api(write_options=SYNCHRONOUS)
        logging.info("InfluxDB write API initialized.")
    except Exception as e:
        logging.exception(f"Failed to initialize InfluxDB write API: {e}")
        # Cannot proceed without write_api
        return

    while True:
        metric = metrics_queue.get() # Blocks until an item is available
        if metric is None:
            logging.info("Pusher thread received None, shutting down.")
            break # Exit signal

        try:
            source = metric.pop("source", "unknown") # Get and remove source identifier

            if source == "srsran":
                # --- Process srsRAN UDP Metrics ---
                if "ue_list" in metric:
                    timestamp = datetime.fromtimestamp(metric["timestamp"], timezone.utc).isoformat()
                    for ue_info in metric["ue_list"]:
                        ue_container = ue_info.get("ue_container", {})
                        rnti = ue_container.pop("rnti", None)
                        pci = ue_container.pop("pci", None)
                        if rnti is None or pci is None:
                            logging.warning(f"Skipping UE entry with missing RNTI/PCI: {ue_info}")
                            continue
                        _influx_push(
                            write_api,
                            bucket=bucket,
                            record={
                                "measurement": "srsran_ue_info",
                                "tags": {
                                    "pci": str(pci),
                                    "rnti": f"{rnti:x}", # Keep hex format
                                    "testbed": testbed,
                                },
                                "fields": dict(convert_integers_to_floats(ue_container).items()),
                                "time": timestamp,
                            },
                        )
                    logging.debug(f"Pushed srsran_ue_info metric batch ({len(metric['ue_list'])} UEs)")

                elif "app_resource_usage" in metric:
                    timestamp = datetime.fromtimestamp(metric["timestamp"], timezone.utc).isoformat()
                    fields = metric.get("app_resource_usage", {})
                    if fields:
                        _influx_push(
                            write_api,
                            bucket=bucket,
                            record={
                                "measurement": "srsran_app_resource_usage",
                                "tags": {"testbed": testbed},
                                "fields": dict(convert_integers_to_floats(fields).items()),
                                "time": timestamp,
                            },
                        )
                        logging.debug("Pushed srsran_app_resource_usage metric")

                elif "ru" in metric and "ofh_cells" in metric["ru"] and metric["ru"]["ofh_cells"]:
                    timestamp = datetime.fromtimestamp(metric["timestamp"], timezone.utc).isoformat()
                    for cell in metric["ru"]["ofh_cells"]:
                        cell_data = cell.get("cell", {})
                        ul_data = cell_data.get("ul", {})
                        received_packets = ul_data.get("received_packets")
                        pci = cell_data.get("pci")
                        if received_packets and pci is not None:
                            _influx_push(
                                write_api,
                                bucket=bucket,
                                record={
                                    "measurement": "srsran_ofh_ul_received_packets",
                                    "tags": {
                                        "pci": str(pci),
                                        "testbed": testbed,
                                    },
                                    "fields": dict(convert_integers_to_floats(received_packets).items()),
                                    "time": timestamp,
                                },
                            )
                    logging.debug(f"Pushed srsran_ofh_ul_received_packets metrics for {len(metric['ru']['ofh_cells'])} cell(s)")
                else:
                    logging.debug(f"Received unhandled srsRAN metric type: {list(metric.keys())}")

            elif source == "ric":
                # --- Process RIC E2 Metrics ---
                # The record should already be formatted by ric_subscription_callback
                if "measurement" in metric and "tags" in metric and "fields" in metric and "time" in metric:
                     # Basic validation before pushing
                    if not metric["fields"]:
                         logging.warning(f"Skipping RIC metric push, no fields found: {metric}")
                         continue
                    _influx_push(
                        write_api,
                        bucket=bucket,
                        record=metric, # Push the pre-formatted record
                    )
                    logging.debug(f"Pushed {metric['measurement']} metric from RIC")
                else:
                    logging.warning(f"Received malformed RIC metric from queue: {metric}")

            else:
                 logging.warning(f"Received metric from unknown source '{source}': {metric}")

        except Exception as e:
            logging.exception(f"Error processing metric in pusher thread: {e} - Metric was: {metric}")
        finally:
             metrics_queue.task_done() # Indicate item processing is complete

    logging.info("InfluxDB pusher thread finished.")
    # Cleanly close the InfluxDB write API
    try:
        write_api.close()
        logging.info("InfluxDB write API closed.")
    except Exception as e:
        logging.exception(f"Error closing InfluxDB write API: {e}")


def _influx_push(write_api: WriteApi, *args, **kwargs) -> None:
    """Robustly pushes data to InfluxDB with retries."""
    while True:
        try:
            # Use record_time_key='time' if not already specified in kwargs['record']
            if 'record' in kwargs and 'record_time_key' not in kwargs:
                kwargs['record_time_key'] = 'time'

            write_api.write(*args, **kwargs)
            logging.debug(f"Successfully pushed record to InfluxDB: {kwargs.get('record', {}).get('measurement', 'N/A')}")
            break
        except (RemoteDisconnected, ConnectionRefusedError, ConnectionResetError) as conn_err:
            logging.warning(f"Connection error pushing data to InfluxDB: {conn_err}. Retrying in 1s...")
            sleep(1)
        except Exception as e:
            logging.exception(f"Unexpected error pushing data to InfluxDB: {e}. Retrying in 1s...")
            # Log the problematic record if possible
            logging.error(f"Record causing error: {kwargs.get('record', 'N/A')}")
            sleep(1)


# --- Utility Functions ---

def convert_integers_to_floats(data):
    """
    Recursively converts all integer values in a dictionary or list to floats.
    Needed because InfluxDB often requires numeric fields to be floats.
    """
    if isinstance(data, dict):
        return {k: convert_integers_to_floats(v) for k, v in data.items()}
    elif isinstance(data, list):
        return [convert_integers_to_floats(elem) for elem in data]
    elif isinstance(data, int):
        return float(data)
    else:
        return data


def _recreate_bucket(client: InfluxDBClient, bucket_name: str) -> None:
    """Deletes and recreates an InfluxDB bucket."""
    logging.warning(f"Attempting to recreate InfluxDB bucket: {bucket_name}")
    try:
        api = client.buckets_api()
        bucket_ref = api.find_bucket_by_name(bucket_name)
        if bucket_ref:
            logging.info(f"Deleting existing bucket: {bucket_name} (ID: {bucket_ref.id})")
            api.delete_bucket(bucket_ref)
            logging.info(f"Bucket {bucket_name} deleted.")
            # Need to create a new bucket object for creation if needed, or just create by name
            org_id = bucket_ref.org_id # Reuse org ID from the deleted bucket
            api.create_bucket(bucket_name=bucket_name, org_id=org_id)
            logging.info(f"Bucket {bucket_name} recreated.")
        else:
            logging.info(f"Bucket {bucket_name} not found, creating it.")
            # Need organization ID - try getting from client config or find default org
            org_api = client.organizations_api()
            orgs = org_api.find_organizations()
            if not orgs or not orgs.orgs:
                 logging.error("Could not find organization ID to create bucket.")
                 return
            org_id = client.org if client.org else orgs.orgs[0].id # Use configured org or first found
            if not org_id:
                 logging.error("Organization ID is missing, cannot create bucket.")
                 return
            logging.info(f"Using Org ID: {org_id} to create bucket {bucket_name}")
            api.create_bucket(bucket_name=bucket_name, org_id=org_id)
            logging.info(f"Bucket {bucket_name} created.")
    except Exception as e:
        logging.exception(f"Error during bucket recreation for '{bucket_name}': {e}")


# --- Argument Parsing (Merged) ---

def _parse_args() -> Tuple[Optional[InfluxDBClient], str, str, bool, int, int, int, int, str, str, int, int, List[int], List[str]]:
    parser = argparse.ArgumentParser(
        description="Receives srsRAN UDP metrics and RIC E2 metrics, pushing both to InfluxDB."
    )
    # srsRAN Listener Args
    parser.add_argument("--udp-port", type=int, required=True, help="UDP Port to listen for srsRAN metrics.")

    # InfluxDB Args
    parser.add_argument(
        "--db-config",
        nargs="*",
        required=True,
        help='InfluxDB configuration as "key=value" pairs (e.g., url=http://localhost:8086 token=mytoken org=myorg). Required: url, token, org.',
    )
    parser.add_argument("--bucket", required=True, help="InfluxDB Bucket to save data.")
    parser.add_argument(
        "--clean-bucket", action="store_true", help="Delete and recreate the InfluxDB bucket before starting."
    )
    parser.add_argument("--testbed", required=True, help="Identifier for the testbed environment (used as a tag).")

    # RIC / xApp Args
    parser.add_argument("--xapp-config", type=str, default='', help="xApp config file path (passed to xAppBase).")
    parser.add_argument("--http-server-port", type=int, default=8090, help="HTTP server listen port for xApp.")
    parser.add_argument("--rmr-port", type=int, default=4560, help="RMR listen port for xApp.")
    parser.add_argument("--e2-node-id", type=str, default='gnb_001_001_00001', help="Target E2 Node ID for subscription.")
    parser.add_argument("--ran-func-id", type=int, default=2, help="Target E2SM KPM RAN function ID.")
    parser.add_argument("--kpm-report-style", type=int, default=1, choices=[1, 2, 3, 4, 5], help="E2SM KPM Report Style for subscription.")
    parser.add_argument("--ue-ids", type=str, default='1', help="Comma-separated UE ID(s) for relevant KPM styles (e.g., 1,2,3).")
    parser.add_argument("--metrics", type=str, default='DRB.UEThpUl,DRB.UEThpDl', help="Comma-separated E2SM KPM metric names.")

    # General Args
    parser.add_argument(
        "--log-level", choices=logging._nameToLevel.keys(), default="INFO", help="Logging level."
    )

    args = parser.parse_args()

    # Parse DB config
    try:
        db_config_dict = {key: value for pair_str in args.db_config for key, value in (pair_str.split("=", 1),)}
        if not all(k in db_config_dict for k in ['url', 'token', 'org']):
            raise ValueError("InfluxDB config requires 'url', 'token', and 'org'.")
        influx_client = InfluxDBClient(**db_config_dict)
        # Verify connection
        try:
             influx_client.ping()
             logging.info("Successfully connected to InfluxDB.")
        except Exception as db_err:
             logging.error(f"Failed to connect to InfluxDB at {db_config_dict.get('url')}: {db_err}")
             # Depending on severity, you might exit here
             influx_client = None # Indicate failure

    except Exception as e:
        parser.error(f"Invalid --db-config format or missing keys: {e}")
        influx_client = None # Should not be reached due to parser.error, but good practice

    # Parse multi-value args for xApp
    ue_ids_list = list(map(int, args.ue_ids.split(","))) if args.ue_ids else []
    metrics_list = args.metrics.split(",") if args.metrics else []


    return (
        influx_client,
        args.bucket,
        args.testbed,
        args.clean_bucket,
        args.udp_port,
        logging._nameToLevel[args.log_level],
        # xApp args
        args.http_server_port,
        args.rmr_port,
        args.xapp_config,
        args.e2_node_id,
        args.ran_func_id,
        args.kpm_report_style,
        ue_ids_list,
        metrics_list,
    )


# --- Main Execution ---

def main():
    """
    Main Entrypoint: Parses args, sets up components, starts threads, and handles shutdown.
    """
    global metrics_queue # Allow modification by close function

    (
        influx_client,
        influx_bucket,
        testbed,
        clean_bucket,
        udp_port,
        log_level,
        http_port,
        rmr_port,
        xapp_config_file,
        e2_node,
        ran_func,
        kpm_style,
        ue_ids,
        ric_metrics,
    ) = _parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)-8s [%(threadName)s] %(message)s",
        level=log_level,
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    logging.info("Starting Integrated srsRAN/RIC Metrics Collector")

    if influx_client is None:
        logging.error("InfluxDB client initialization failed. Exiting.")
        return # Cannot proceed without DB client

    if clean_bucket:
        _recreate_bucket(influx_client, influx_bucket)

    # --- Initialize Components ---
    udp_listener_thread = Thread(target=_start_metric_server, args=(udp_port,), name="UDPListenerThread")
    influx_pusher_thread = Thread(target=_publish_data, args=(influx_client, influx_bucket, testbed), name="InfluxPusherThread")

    # Initialize the xApp (pass queue, bucket, testbed for callback)
    try:
        ric_xapp = IntegratedXapp(xapp_config_file, http_port, rmr_port, influx_bucket, testbed)
        ric_xapp.e2sm_kpm.set_ran_func_id(ran_func) # Set RAN function ID before subscribing
        logging.info("IntegratedXapp instance created.")
    except Exception as e:
        logging.exception(f"Failed to initialize IntegratedXapp: {e}")
        # Decide if you can continue without RIC functionality
        ric_xapp = None # Mark as unavailable


    # --- Signal Handling ---
    shutdown_requested = False
    def close(signum=None, frame=None):
        nonlocal shutdown_requested
        if shutdown_requested:
            return
        shutdown_requested = True
        logging.info("Shutdown requested (Signal: {})...".format(signal.Signals(signum).name if signum else 'N/A'))

        # 1. Stop xApp gracefully (unsubscribe, etc.)
        if ric_xapp:
            try:
                logging.info("Requesting xApp shutdown...")
                # Use the signal handler provided by xAppBase
                ric_xapp.signal_handler(signal.SIGTERM, None)
            except Exception as e:
                logging.exception(f"Error during xApp shutdown: {e}")

        # 2. Stop UDP listener thread gracefully
        # Send an empty packet to the UDP port to unblock the recv() call
        logging.info("Sending shutdown signal to UDP listener...")
        try:
            with socket.socket(family=socket.AF_INET, type=socket.SOCK_DGRAM) as sock:
                 # Send to localhost where the server is bound
                sock.sendto(b"", ("127.0.0.1", udp_port))
        except Exception as e:
            logging.error(f"Could not send shutdown signal to UDP listener on port {udp_port}: {e}")
            # The thread might hang on recv() if the signal isn't sent

        # 3. Signal the pusher thread to exit by putting None on the queue
        logging.info("Signaling InfluxDB pusher thread to exit...")
        metrics_queue.put(None)

        # Note: Threads will be joined later

    signal.signal(signal.SIGINT, close)
    signal.signal(signal.SIGTERM, close)
    # SIGQUIT might be relevant for xAppBase depending on its implementation
    if hasattr(signal, 'SIGQUIT'):
         signal.signal(signal.SIGQUIT, close)


    # --- Start Threads and xApp ---
    logging.info("Starting UDP listener thread...")
    udp_listener_thread.start()

    logging.info("Starting InfluxDB pusher thread...")
    influx_pusher_thread.start()

    if ric_xapp:
        try:
            # This might block or start background tasks depending on xAppBase implementation
            # Run it *after* our threads are started. If it blocks the main thread,
            # it might need to be run in its own thread too.
            logging.info("Starting xApp subscription logic...")
            ric_xapp.start(e2_node, kpm_style, ue_ids, ric_metrics)
            logging.info("xApp start method called.")
        except Exception as e:
            logging.exception("Error occurred during xApp start. RIC metrics might not be collected.")
            # Maybe call close() here if RIC functionality is critical
            # close()

    # --- Wait for Threads to Finish ---
    logging.info("Waiting for threads to complete...")
    # Wait for UDP listener first (it should exit after receiving the signal)
    udp_listener_thread.join(timeout=10) # Add a timeout
    if udp_listener_thread.is_alive():
        logging.warning("UDP listener thread did not exit cleanly.")

    # Wait for pusher thread (it should exit after processing None from queue)
    influx_pusher_thread.join(timeout=10) # Add a timeout
    if influx_pusher_thread.is_alive():
         logging.warning("InfluxDB pusher thread did not exit cleanly.")


    # Final cleanup for InfluxDB client
    try:
        influx_client.close()
        logging.info("InfluxDB client closed.")
    except Exception as e:
        logging.exception(f"Error closing InfluxDB client: {e}")

    logging.info("Application finished.")


if __name__ == "__main__":
    main()
