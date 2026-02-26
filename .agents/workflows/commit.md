---
description: Commit current changes with tests passing
---

// turbo-all

1. Run the full test suite first:
   ```bash
   conda run -n trader python -m pytest tests/ -v --tb=short -m "not slow"
   ```

2. If all tests pass, stage and commit:
   ```bash
   git add -A && git status
   ```

3. Commit with a descriptive message based on what changed:
   ```bash
   git commit -m "<descriptive commit message>"
   ```

4. Show the commit summary:
   ```bash
   git log -1 --stat
   ```
