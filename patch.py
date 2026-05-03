import re

with open('c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/src/data_processor.py', encoding='utf-8') as f:
    lines = f.read()

# Find process_hourset_06
m = re.search(r'    def process_hourset_06\(self\).*?return df\n', lines, re.DOTALL)
if not m:
    print("Could not find process_hourset_06")
    exit(1)

code = m.group(0)

# Make process_hourset_07
code7 = code.replace('process_hourset_06', 'process_hourset_07')
code7 = code7.replace('HOURSET_03', 'HOURSET_07') # In the print statements
code7 = code7.replace('HourSet_06', 'HourSet_07')

# We only want 24H target instead of 72H and 120H
# The loop looks like:
#         for tp_mult in [1.5, 2.0, 2.5]:
#             tp_label = str(tp_mult).replace(".", "p")
#             for horizon_h in [72, 120]:
#                 df = self.add_triple_barrier_target(

replacement = """        for tp_mult in [1.5]:
            tp_label = "1p5"
            for horizon_h in [24]:
                df = self.add_triple_barrier_target("""
                
code7 = re.sub(r'        for tp_mult in \[1\.5, 2\.0, 2\.5\]:.*?for horizon_h in \[72, 120\]:\s*df = self\.add_triple_barrier_target\(', replacement, code7, flags=re.DOTALL)

# Add it after process_hourset_06
if 'def process_hourset_07' not in lines:
    lines = lines.replace(code, code + '\n' + code7)
    with open('c:/Users/bwang/Documents/GitHub/CL_Analyst_Development/src/data_processor.py', 'w', encoding='utf-8') as f:
        f.write(lines)
    print("Patched successfully")
else:
    print("Already patched")
