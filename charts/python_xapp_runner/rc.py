#!/usr/bin/env python3

import time
import datetime
import argparse
import signal
from lib.xAppBase import xAppBase

class MyXapp(xAppBase):
    def __init__(self, config, http_server_port, rmr_port):
        super(MyXapp, self).__init__(config, http_server_port, rmr_port)
        pass

    # Mark the function as xApp start function using xAppBase.start_function decorator.
    # It is required to start the internal msg receive loop.
    @xAppBase.start_function
    def start(self, e2_node_id, ue_id, min_prb, max_prb, plmn_string):
        while self.running:
            try:
                user_input = input("\nEnter PRB_min_ratio and PRB_max_ratio: ").strip()
                if not user_input:
                    continue
    
                try:
                    min_prb_ratio_str, max_prb_ratio_str = user_input.split()
                    min_prb_ratio = int(min_prb_ratio_str)
                    max_prb_ratio = int(max_prb_ratio_str)
                except ValueError:
                    print("❌ Invalid input. Please enter two integers separated by a space.")
                    continue
    
                if (min_prb_ratio, max_prb_ratio) == (last_min_prb, last_max_prb):
                    print("⚠️  Same PRB values as last time. No control message sent.")
                    continue
    
                last_min_prb = min_prb_ratio
                last_max_prb = max_prb_ratio
    
                current_time = datetime.datetime.now()
                print("{} ✅ Sending RIC Control Request to E2 node ID: {} for UE ID: {}, PRB_min_ratio: {}, PRB_max_ratio: {}".format(
                    current_time.strftime("%H:%M:%S"), e2_node_id, ue_id, min_prb_ratio, max_prb_ratio))
    
                self.e2sm_rc.control_slice_level_prb_quota(
                    e2_node_id, ue_id,
                    min_prb_ratio, max_prb_ratio,
                    plmn_string,
                    dedicated_prb_ratio=100,
                    ack_request=1
                )
    
            except KeyboardInterrupt:
                print("\n🛑 PRB control loop stopped.")
                self.running = False

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='My example xApp')
    parser.add_argument("--config", type=str, default='', help="xApp config file path")
    parser.add_argument("--http_server_port", type=int, default=8090, help="HTTP server listen port")
    parser.add_argument("--rmr_port", type=int, default=4560, help="RMR port")
    parser.add_argument("--e2_node_id", type=str, default='gnbd_999_092_00019b_1', help="E2 Node ID")
    parser.add_argument("--ran_func_id", type=int, default=3, help="E2SM RC RAN function ID")
    parser.add_argument("--ue_id", type=int, default=0, help="UE ID")
    parser.add_argument("--min_prb", type=int, help="Minimum PRB")
    parser.add_argument("--max_prb", type=int, help="Maximum PRB")
    parser.add_argument("--plmn_string", type=int, help="PLMN string")


    args = parser.parse_args()
    config = args.config
    e2_node_id = args.e2_node_id # TODO: get available E2 nodes from SubMgr, now the id has to be given.
    ran_func_id = args.ran_func_id # TODO: get available E2 nodes from SubMgr, now the id has to be given.
    ue_id = args.ue_id
    min_prb = args.min_prb
    max_prb = args.max_prb
    plmn_string = args.plmn_string
    
    # Create MyXapp.
    myXapp = MyXapp(config, args.http_server_port, args.rmr_port)
    myXapp.e2sm_rc.set_ran_func_id(ran_func_id)

    # Connect exit signals.
    signal.signal(signal.SIGQUIT, myXapp.signal_handler)
    signal.signal(signal.SIGTERM, myXapp.signal_handler)
    signal.signal(signal.SIGINT, myXapp.signal_handler)

    # Start xApp.
    myXapp.start(e2_node_id, ue_id, min_prb, max_prb, plmn_string)
