"""Non-destructive configuration creation and prerequisite validation."""
import secrets
import shutil
import subprocess
from pathlib import Path

if not shutil.which("docker"):
    raise SystemExit("Install Docker Desktop (macOS) or Docker Engine with Compose (Linux).")
subprocess.run(["docker", "compose", "version"], check=True)
subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
env = Path(".env")
if not env.exists():
    password = secrets.token_hex(24)
    env.write_text(Path(".env.example").read_text().replace("yureatlas_local", password))
    env.chmod(0o600)
print("Configuration ready. Existing .env and research data were preserved.")
