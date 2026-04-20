---
description: How to handle terminal output on this Windows/PowerShell environment to avoid truncation and wrapping issues
---

# Terminal Output Best Practices

The terminal in this environment has a narrow display width (~50 chars visible). Long output lines get wrapped and interleaved, making multi-line results unreadable. This is a **recurring problem** — follow these rules to avoid wasting time on workarounds.

## Rules

### 1. Never rely on terminal output for multi-line results
If a command produces more than ~5 lines of output, **write results to a file and read it back** instead of printing to stdout.

```python
# ❌ BAD — output will be mangled
print(long_report)

# ✅ GOOD — write to file, then read with view_file tool
with open("tmp/result.txt", "w", encoding="utf-8") as f:
    f.write(long_report)
```

Then use the `view_file` tool on the output file to read it cleanly.

### 2. For quick Python diagnostics, keep lines SHORT
If you must print to terminal, keep each `print()` under ~45 characters:

```python
# ❌ BAD
print(f"  {table}: {count:,} rows  [{min_ts} -> {max_ts}]")

# ✅ GOOD
print(f"  {table} = {count} rows")
print(f"    from: {min_ts}")
print(f"    to:   {max_ts}")
```

### 3. For scripts that produce reports, always write to tmp/
Create a temp file and use `view_file` to read it:

```python
# In the script:
with open("tmp/report.txt", "w", encoding="utf-8") as f:
    f.write(report_text)

# Then use the view_file tool on tmp/report.txt
```

> **Important**: Use `encoding="utf-8"` explicitly. PowerShell redirection (`>`) produces UTF-16LE files which `view_file` cannot read.

### 4. PowerShell redirection creates UTF-16 — avoid it
```powershell
# ❌ BAD — creates UTF-16LE file that view_file rejects
python script.py > output.txt

# ✅ GOOD — have Python write the file itself with utf-8
python script.py  # script writes to tmp/output.txt internally
```

### 5. For one-liner checks, use simple short prints
```python
# ✅ This works fine for quick checks
python -c "import pandas as pd; df = pd.read_parquet('file.parquet'); print(len(df))"
```

### 6. `conda run` + emoji = UnicodeEncodeError (CRITICAL — will crash the whole process)
`conda run` acts as a stdout proxy. It captures all subprocess output and re-prints it using the **parent shell's encoding (cp1252)**. Any Unicode character outside cp1252 (emoji, ∙, ±, etc.) causes a hard crash at `conda\cli\main_run.py` — the `try/except UnicodeEncodeError` in Python code **does NOT protect against this** because the error happens in the conda proxy after Python has already exited.

**Two-layer fix — apply both:**

1. **Workflow layer** — set env var before every `conda run`:
```powershell
# ✅ Set before all conda run commands in workflow files
$env:PYTHONUTF8 = "1"
conda run -n trader python tests/smoke_test_pipeline.py
```

2. **Script layer** — force UTF-8 stdout at the top of any script that prints Unicode:
```python
import sys
# Force UTF-8 — must come before any print() call
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

> **Rule**: Every workflow step using `conda run` MUST have `$env:PYTHONUTF8 = "1"` on the line before it. Every script that uses emoji or non-ASCII characters MUST have the `reconfigure` guard at the top.

