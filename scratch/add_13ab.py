import re
import ast

def process_file():
    with open("src/data_processor.py", "r", encoding="utf-8") as f:
        content = f.read()

    # 1. Update Dispatcher
    dispatcher_regex = r'(elif self\.dataset_version == "HourSet_12":\s+return self\.process_hourset_12\(\))'
    dispatcher_replacement = r'\1\n        elif self.dataset_version == "HourSet_13A":\n            return self.process_hourset_13a(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))\n        elif self.dataset_version == "HourSet_13B":\n            return self.process_hourset_13b(exec_ohlcv_path=kwargs.get("exec_ohlcv_path"))'
    
    content = re.sub(dispatcher_regex, dispatcher_replacement, content)

    # 2. Extract process_hourset_09
    pattern_09 = re.compile(r'    def process_hourset_09\(self, exec_ohlcv_path: Optional\[str\] = None\) -> pd\.DataFrame:.*?    def process_hourset_10', re.DOTALL)
    match_09 = pattern_09.search(content)
    code_09 = match_09.group(0).replace('    def process_hourset_10', '')
    
    # Modify 09 -> 13a
    code_13a = code_09.replace('def process_hourset_09(', 'def process_hourset_13a(')
    code_13a = code_13a.replace('HOURSET_09', 'HOURSET_13A')
    code_13a = code_13a.replace('HourSet_09', 'HourSet_13A')
    
    # 3. Extract process_hourset_10
    pattern_10 = re.compile(r'    def process_hourset_10\(self, exec_ohlcv_path: Optional\[str\] = None\) -> pd\.DataFrame:.*?    def process_hourset_11', re.DOTALL)
    match_10 = pattern_10.search(content)
    code_10 = match_10.group(0).replace('    def process_hourset_11', '')
    
    # Modify 10 -> 13b
    code_13b = code_10.replace('def process_hourset_10(', 'def process_hourset_13b(')
    code_13b = code_13b.replace('HOURSET_10', 'HOURSET_13B')
    code_13b = code_13b.replace('HourSet_10', 'HourSet_13B')

    # 4. Target Injection Code
    target_injection = """
        # --- NEW TARGETS (13A/13B) ---
        # 3H horizon: 2x1
        df = self.add_triple_barrier_target(
            df, prefix="TARGET_TRIPLE_2x1_3H",
            tp_atr_mult=2.0, sl_atr_mult=1.0, max_horizon=3, atr_period=14
        )
        
        # 36H and 48H horizons
        long_configs = [
            (4.0, 1.0, "4x1"),
            (5.0, 1.0, "5x1"),
            (6.0, 2.0, "6x2"),
            (8.0, 2.0, "8x2")
        ]
        for tp_mult, sl_mult, tag in long_configs:
            for horizon_h in [36, 48]:
                df = self.add_triple_barrier_target(
                    df,
                    prefix=f"TARGET_TRIPLE_{tag}_{horizon_h}H",
                    tp_atr_mult=tp_mult,
                    sl_atr_mult=sl_mult,
                    max_horizon=horizon_h,
                    atr_period=14,
                )
"""

    # Inject into 13a
    insert_pos_13a = code_13a.find('        # 6c: Continuous return targets')
    code_13a = code_13a[:insert_pos_13a] + target_injection + code_13a[insert_pos_13a:]
    
    # Inject into 13b
    insert_pos_13b = code_13b.find('        df = self.add_return_target(df, horizons=[6, 12, 24, 72, 120])')
    code_13b = code_13b[:insert_pos_13b] + target_injection + code_13b[insert_pos_13b:]

    # 5. Add 13a and 13b to the end of the file right before process_hour4set_01
    insert_pos_final = content.find('    def process_hour4set_01(self) -> pd.DataFrame:')
    new_content = content[:insert_pos_final] + code_13a + code_13b + content[insert_pos_final:]

    with open("src/data_processor.py", "w", encoding="utf-8") as f:
        f.write(new_content)
    
    print("Successfully patched src/data_processor.py")

if __name__ == "__main__":
    process_file()
