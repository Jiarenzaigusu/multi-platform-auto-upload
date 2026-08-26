# Copy this file to mpau.local.ps1. The local file is ignored by Git.
$env:MPAU_DATA_DIR = "D:\MPAU\data"
$env:MPAU_AGENT_INSTALLER_PATH = "D:\MPAU\releases\MPAU-Agent-Setup.exe"
# Edge and browser capacity are configured on each user's local agent, not here.
$env:MPAU_SESSION_SECONDS = "43200"
$env:MPAU_MAX_UPLOAD_REQUEST_BYTES = "21474836480"
$env:MPAU_MAX_MEDIA_TOTAL_BYTES = "107374182400"
$env:MPAU_MAX_MEDIA_FILES = "1000"

# Direct FastAPI deployment. Replace 10.31.108.221 with your server IP or domain.
$env:MPAU_BIND_HOST = "0.0.0.0"
$env:MPAU_PORT = "8788"
$env:MPAU_ALLOWED_HOSTS = "10.31.108.221,127.0.0.1,localhost"
$env:MPAU_ALLOWED_ORIGINS = "http://10.31.108.221:8788,http://127.0.0.1:8788,http://localhost:8788"

# Keep this false in normal operation. Set it to true only for the first remote administrator setup.
$env:MPAU_ALLOW_REMOTE_BOOTSTRAP = "false"
