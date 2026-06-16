import subprocess
import os
import sys

env = os.environ.copy()
env["CLOUDSDK_CORE_DISABLE_PROMPTS"] = "1"

cmd = [
    "powershell",
    "-ExecutionPolicy", "Bypass",
    "-File", ".\\gcp\\run_sweep_batch.ps1",
    "-ManifestPath", "configs\\sweep_batch_short_hourset13a_canary.json",
    "-Zone", "us-west1-a,us-west1-b,us-west1-c,us-central1-a,us-central1-b,us-central1-c,us-central1-f"
]

with open("runner_stdout.log", "w") as out:
    proc = subprocess.Popen(
        cmd,
        env=env,
        stdout=out,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
    )
print(f"Spawned background process with PID {proc.pid}")
