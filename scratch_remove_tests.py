with open('tests/test_reconnection.py', 'r') as f:
    lines = f.readlines()

# Delete lines 220 to 272 (inclusive), which is index 219 to 272
del lines[219:272]

with open('tests/test_reconnection.py', 'w') as f:
    f.writelines(lines)
