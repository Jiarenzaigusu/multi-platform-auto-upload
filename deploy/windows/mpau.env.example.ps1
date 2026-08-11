# Copy this file to mpau.local.ps1. The local file is ignored by Git.
$env:MPAU_DATA_DIR = "D:\MPAU\data"
$env:MPAU_AGENT_INSTALLER_PATH = "D:\MPAU\releases\MPAU-Agent-Setup.exe"
# Edge and browser capacity are configured on each user's local agent, not here.
$env:MPAU_SESSION_SECONDS = "43200"
$env:MPAU_MAX_UPLOAD_REQUEST_BYTES = "21474836480"
$env:MPAU_MAX_MEDIA_TOTAL_BYTES = "107374182400"
$env:MPAU_MAX_MEDIA_FILES = "1000"

# Replace this example hostname with the internal HTTPS hostname.
$env:MPAU_ALLOWED_HOSTS = "mpau.internal.example.com,127.0.0.1,localhost"
$env:MPAU_ALLOWED_ORIGINS = "https://mpau.internal.example.com"
$env:MPAU_SECURE_COOKIES = "true"
