import sys
import re

def main():
    path = 'src/live_execution/live_trader.py'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 1. Remove verbose logs from regex
    content = re.sub(r'\s*r"\|updatePortfolio:"\n?', '', content)
    content = re.sub(r'\s*r"\|position:"\n?', '', content)

    # 2. Fix logger filter
    content = content.replace(
        'logging.getLogger("ib_insync").addFilter(CLOnlyLogFilter())',
        'logging.getLogger("ib_insync.wrapper").addFilter(CLOnlyLogFilter())'
    )

    lines = content.splitlines()
    start_idx = -1
    end_idx = -1
    for i, line in enumerate(lines):
        if line.strip() == '# Position':
            start_idx = i
            break
    
    if start_idx != -1:
        for i in range(start_idx, len(lines)):
            if 'self.manager.ib.isConnected(),' in lines[i]:
                end_idx = i + 1
                break
    
    if start_idx != -1 and end_idx != -1:
        new_heartbeat = [
            '        # Position and PNL lookup',
            '        try:',
            '            # Pnl accumulation for the execution symbol',
            '            unr_pnl, real_pnl = 0.0, 0.0',
            '            pos = 0.0',
            '            if getattr(self.manager, "ib", None) and self.manager.ib.isConnected():',
            '                for item in self.manager.ib.portfolio():',
            '                    if getattr(item.contract, "symbol", "") == self._execution_symbol:',
            '                        pos += getattr(item, "position", 0.0)',
            '                        unr_pnl += getattr(item, "unrealizedPNL", 0.0) or 0.0',
            '                        real_pnl += getattr(item, "realizedPNL", 0.0) or 0.0',
            '            ',
            '            pos_str = f"{pos:g} contracts" if pos != 0 else "FLAT"',
            '            pnl_str = f" | unr_pnl=${unr_pnl:,.2f} | real_pnl=${real_pnl:,.2f}"',
            '        except Exception:',
            '            pos_str = "unknown"',
            '            pnl_str = ""',
            '',
            '        log.info(',
            '            "HEARTBEAT: alive | last_bar=%s | market=%s | position=%s%s | connected=%s",',
            '            last_bar_str,',
            '            market_status,',
            '            pos_str,',
            '            pnl_str,',
            '            self.manager.ib.isConnected() if getattr(self.manager, "ib", None) else False,',
            '        )'
        ]
        
        lines = lines[:start_idx] + new_heartbeat + lines[end_idx+1:]
        content = '\n'.join(lines) + '\n'
        
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        print("Successfully updated live_trader.py!")
    else:
        print(f"Error: Could not find heartbeat block. start={start_idx}, end={end_idx}")

if __name__ == '__main__':
    main()
