import os
import asn1tools

class e2sm_kpm_packer(object):
    def __init__(self):
        super(e2sm_kpm_packer, self).__init__()
        self.my_dir = os.path.dirname(os.path.abspath(__file__))
        asn1_files = [self.my_dir+'/e2sm-v5.00.asn', self.my_dir+'/e2sm-kpm-v4.00.asn']
        self.asn1_compiler = asn1tools.compile_files(asn1_files,'per')

    def pack_event_trigger_def(self, reportingPeriod):
        e2sm_kpm_trigger_def = {'eventDefinition-formats': ('eventDefinition-Format1', {'reportingPeriod': reportingPeriod})}
        e2sm_kpm_trigger_def = self.asn1_compiler.encode('E2SM-KPM-EventTriggerDefinition', e2sm_kpm_trigger_def)
        return e2sm_kpm_trigger_def

    def _pack_meas_info_list(self, metric_names):
        measInfoList = []
        # TODO: pack also labels
        for metric_name in metric_names:
            metric_def = {'measType': ('measName', metric_name), 'labelInfoList': [{'measLabel': {'noLabel': 'true'}}]}
            measInfoList.append(metric_def)
        return measInfoList

    def _pack_ue_id_list(self, ue_ids):
        matchingUEidList = []
        for ue_id in ue_ids:
            matchingUEidList.append({'ueID': ('gNB-DU-UEID', {'gNB-CU-UE-F1AP-ID': ue_id})})
        return matchingUEidList

    def _pack_matching_conds_list(self, matchingConds):
        matchingCondList = matchingConds
        return matchingCondList

    def _pack_matching_ue_conds_list(self, matchingUeConds):
        matchingUeCondList = matchingUeConds
        return matchingUeCondList

      
    def _pack_meas_info_list(self, metric_names):
        # This method would be part of your e2sm_kpm_compiler.py or a similar packing utility
        measInfoList_asn1_struct = [] # This list will hold the dicts for each MeasurementInfoItem

        metrics_requiring_slice_id_label = {"DRB.AirIfDelayDist"}
        
        snssai_data_explicit_no_sd = {'sST': b'\x01', 'sD': b'\xff\xff\xff'}
        snssai_data_sst_only = {
            'sST': b'\x01'
            # sD is omitted
        }
        test_sst_A = {'sST': b'A'}
        test_sst_A_int = {'sST': bytes([65])}
        print(self.asn1_compiler.encode('S-NSSAI', test_sst_A))
        print(self.asn1_compiler.encode('S-NSSAI', test_sst_A_int))
        try:
            encoded_bytes1 = self.asn1_compiler.encode('S-NSSAI', snssai_data_explicit_no_sd)
            print(f"Encoded S-NSSAI (explicit no SD): {list(encoded_bytes1)}") # Print as list of ints
        
            encoded_bytes2 = self.asn1_compiler.encode('S-NSSAI', snssai_data_sst_only)
            print(f"Encoded S-NSSAI (SST only): {list(encoded_bytes2)}")
        
        except Exception as e:
            print(f"Error encoding S-NSSAI directly: {e}")
        
        for metric_name_str in metric_names:
            label_info_list_for_this_metric = [] # This will be the value for 'labelInfoList' key

            if metric_name_str in metrics_requiring_slice_id_label:
                # Construct LabelInfoList for DRB.AirIfDelayDist with SliceID
                # 1. Create the S-NSSAI structure
                # In _pack_meas_info_list, for DRB.AirIfDelayDist:
                s_nssai_struct = {
                    'sST': b'\x01',
                    'sD': b'\xff\xff\xff'  # Explicitly no specific SD
                }
                measurement_label_struct = {'sliceID': s_nssai_struct}
                label_info_item_struct = {'measLabel': measurement_label_struct}
                label_info_list_for_this_metric.append(label_info_item_struct)
                
            else: # For other metrics like DRB.UEThpDl, DRB.UEThpUl
                # Use noLabel = true (assuming this has been working for them)
                measurement_label_struct = {'noLabel': 'true'} # Assuming ENUMERATED 'true'
                label_info_item_struct = {'measLabel': measurement_label_struct}
                label_info_list_for_this_metric.append(label_info_item_struct)
            
            # Construct the MeasurementInfoItem for the current metric
            # Remember measType is a CHOICE, often represented as a tuple
            # ('chosenFieldName', value_for_that_field)
            metric_def_asn1_struct = {
                'measType': ('measName', metric_name_str), 
                'labelInfoList': label_info_list_for_this_metric
                # 'matchCondReportList' is OPTIONAL and omitted here
            }
            
            measInfoList_asn1_struct.append(metric_def_asn1_struct)
            
        return measInfoList_asn1_struct # This is the list ready to be part of ActionDefinition-Format1

    
    def pack_action_def_format1(self, metric_names, granulPeriod=100):
        if not isinstance(metric_names, list):
            metric_names = [metric_names]

        measInfoList = self._pack_meas_info_list(metric_names)

        action_def = {'ric-Style-Type': 1,
                      'actionDefinition-formats': ('actionDefinition-Format1', {
                          'measInfoList': measInfoList, 
                          'granulPeriod': granulPeriod
                          })
                     }
        action_def = self.asn1_compiler.encode('E2SM-KPM-ActionDefinition', action_def)
        return action_def
    
    def pack_action_def_format2(self, ue_id, metric_names, granulPeriod=100):
        if not isinstance(metric_names, list):
            metric_names = [metric_names]

        ue_id = self._pack_ue_id_list([ue_id])
        ue_id = tuple(ue_id[0]['ueID']) # extract as there is only 1 UE

        measInfoList = self._pack_meas_info_list(metric_names)
        action_def = {'ric-Style-Type': 2,
                      'actionDefinition-formats': ('actionDefinition-Format2', {
                          'ueID': ue_id,
                          'subscriptInfo': {
                              'measInfoList': measInfoList, 
                              'granulPeriod': granulPeriod}
                       })
                     }
        action_def = self.asn1_compiler.encode('E2SM-KPM-ActionDefinition', action_def)
        return action_def

    def pack_action_def_format3(self, matchingConds, metric_names, granulPeriod=100):
        if not isinstance(metric_names, list):
            metric_names = [metric_names]

        if (len(metric_names) > 1):
            print("Currently only 1 metric can be requested in E2SM-KPM Report Style 3")
            exit(1)

        matchingCondList = self._pack_matching_conds_list(matchingConds)

        action_def = {'ric-Style-Type': 3, 
                      'actionDefinition-formats': ('actionDefinition-Format3', {
                        'measCondList': [
                          {'measType': ('measName', metric_names[0]), 'matchingCond': matchingCondList}
                        ], 
                      'granulPeriod': granulPeriod})
                     }
        action_def = self.asn1_compiler.encode('E2SM-KPM-ActionDefinition', action_def)
        return action_def

    def pack_action_def_format4(self, matchingUeConds, metric_names, granulPeriod=100):
        if not isinstance(metric_names, list):
            metric_names = [metric_names]

        measInfoList = self._pack_meas_info_list(metric_names)
        matchingUeCondList = self._pack_matching_ue_conds_list(matchingUeConds)

        action_def = {'ric-Style-Type': 4, 
                      'actionDefinition-formats': ('actionDefinition-Format4', 
                        {'matchingUeCondList': matchingUeCondList,
                        'subscriptionInfo': {
                            'measInfoList': measInfoList, 
                            'granulPeriod': granulPeriod
                        }}
                     )}
        action_def = self.asn1_compiler.encode('E2SM-KPM-ActionDefinition', action_def)
        return action_def

    def pack_action_def_format5(self, ue_ids, metric_names, granulPeriod=100):
        if not isinstance(metric_names, list):
            metric_names = [metric_names]

        matchingUEidList = self._pack_ue_id_list(ue_ids)
        measInfoList = self._pack_meas_info_list(metric_names)

        action_def = {'ric-Style-Type': 5,
                       'actionDefinition-formats': ('actionDefinition-Format5', 
                        {'matchingUEidList': matchingUEidList,
                        'subscriptionInfo': {
                            'measInfoList': measInfoList,
                            'granulPeriod': granulPeriod}
                        })
                     }
        action_def = self.asn1_compiler.encode('E2SM-KPM-ActionDefinition', action_def)
        return action_def

    def unpack_indication_header_format1(self, msg_bytes):
        indication_hdr = self.asn1_compiler.decode('E2SM-KPM-IndicationHeader-Format1', msg_bytes)
        return indication_hdr

    def unpack_indication_header(self, msg_bytes):
        return self.unpack_indication_header_format1(msg_bytes)

    def unpack_indication_message(self, msg_bytes):
        indication_msg = self.asn1_compiler.decode('E2SM-KPM-IndicationMessage', msg_bytes)
        return indication_msg
