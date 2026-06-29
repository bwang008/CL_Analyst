import json

with open('configs/master/DataMap_CL_HourSet_14A.json', 'r') as f:
    config_a = json.load(f)

with open('configs/master/DataMap_CL_HourSet_14B.json', 'r') as f:
    config_b = json.load(f)

config_b['data_workflow']['targets'] = config_a['data_workflow']['targets']

with open('configs/master/DataMap_CL_HourSet_14B.json', 'w') as f:
    json.dump(config_b, f, indent=2)

print('Successfully copied targets from 14A to 14B')
