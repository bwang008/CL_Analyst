---
description: Fix CRLF line endings for PowerShell scripts after creation or modification
---

# Fix PowerShell Line Endings

When creating or modifying `.ps1` files on Windows, the file-writing tools save with LF (Unix) line endings. PowerShell requires CRLF (Windows) line endings to parse correctly.

## After creating or editing any `.ps1` file, run:

// turbo
```
python -c "p='<RELATIVE_PATH_TO_PS1_FILE>'; d=open(p,'rb').read().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'); open(p,'wb').write(d)"
```

Replace `<RELATIVE_PATH_TO_PS1_FILE>` with the actual path to the `.ps1` file that was just created or modified.

## Example:
```
python -c "p='gcp/gcp_deploy_canary.ps1'; d=open(p,'rb').read().replace(b'\r\n',b'\n').replace(b'\n',b'\r\n'); open(p,'wb').write(d)"
```

## Why this is needed:
- The `write_to_file` and `replace_file_content` tools save files with LF line endings
- PowerShell on Windows cannot parse `.ps1` files with LF-only endings
- The symptom is `The string is missing the terminator: "` parser errors
- This one-liner normalizes all line endings to CRLF
