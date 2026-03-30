import ast

with open('src/live_execution/live_trader.py', 'r', encoding='utf-8') as f:
    tree = ast.parse(f.read())

for node in tree.body:
    if isinstance(node, ast.ClassDef) and node.name == 'LiveTrader':
        print(f"Methods for {node.name}:")
        for m in node.body:
            if isinstance(m, ast.FunctionDef):
                print(f" - {m.name}")
