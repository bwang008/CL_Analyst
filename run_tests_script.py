import subprocess
import sys

def run():
    try:
        # Run pytest and capture output
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )
        
        with open("pytest_script_out.txt", "w", encoding="utf-8") as f:
            for line in process.stdout:
                f.write(line)
                f.flush()
                print(line, end="")
        
        process.wait()
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    run()
