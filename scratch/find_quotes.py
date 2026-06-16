import re

def find_unbalanced_quotes(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    in_string = False
    for i, line in enumerate(lines):
        line = line.strip()
        # Remove escaped quotes
        line = line.replace('\\"', '')
        line = line.replace('`"', '') # powershell escape
        
        # Count quotes
        num_quotes = line.count('"')
        if num_quotes % 2 != 0:
            print(f"Line {i+1}: {line}")
            in_string = not in_string
            
if __name__ == '__main__':
    find_unbalanced_quotes('gcp/run_sweep_batch.ps1')
