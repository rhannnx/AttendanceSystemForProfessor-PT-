# SBC PtAttendance

Django-based attendance tracking for part-time professors.

## Features

- Professor time-in and time-out
- Daily signed QR codes
- School-network/WiFi enforcement
- Attendance sessions and late calculation
- HR dashboard, reports, analytics, and audit logs
- Professor portal
- SMS/email HR notifications

## Requirements

- Windows PowerShell
- Python virtual environment at `..\myenv`
- PostgreSQL
- Waitress
- Caddy

Install Python dependencies:

```powershell
& ..\myenv\Scripts\python.exe -m pip install -r requirements.txt
```

## Environment

Copy `.env.example` to `.env` and fill in the real values. At minimum:

```dotenv
DEBUG=False
ALLOWED_HOSTS=127.0.0.1,localhost,192.168.137.1
TRUST_X_FORWARDED_FOR=True
WIFI_BYPASS=False
ALLOWED_IP_RANGES=127.0.0.1,192.168.1.,192.168.137.
```

Keep `WIFI_BYPASS=False` for the pilot. `TRUST_X_FORWARDED_FOR=True` is required because Caddy supplies the real client IP.

## HTTPS/HTTP pilot note

This repository currently documents an **HTTP pilot on an isolated Windows hotspot**. It preserves the real per-phone WiFi check, but traffic between phones and the laptop is unencrypted.

Use this only for a short, supervised pilot on a strong-password hotspot with your professor's approval. Do not expose the app to the public internet. For permanent use, deploy on school-managed HTTPS infrastructure.

## Run locally

For local development, set `DEBUG=True` so Django serves static and media files. For the pilot, keep `DEBUG=False` and serve through Caddy.

```powershell
& ..\myenv\Scripts\python.exe manage.py migrate
& ..\myenv\Scripts\python.exe manage.py runserver
```

## Pilot deployment (HTTP hotspot)

### 1. Start the Windows hotspot

Use a strong password. Confirm the laptop hotspot address is `192.168.137.1`.

### 2. Collect static files

```powershell
& ..\myenv\Scripts\python.exe manage.py collectstatic --noinput
```

### 3. Start Waitress

Run from this project directory:

```powershell
& ..\myenv\Scripts\python.exe deploy\serve_waitress.py
```

This launcher binds Waitress to `127.0.0.1:8000` and trusts only Caddy's forwarded client-IP headers. Do not replace it with a bare `waitress-serve` command; Waitress would clear the forwarded IP by default and every request would look like it came from the laptop.

### 4. Start Caddy

Install Caddy outside the repository, for example at `C:\Tools\caddy\caddy.exe`. Run from this project directory so the relative static/media paths resolve:

```powershell
& "C:\Tools\caddy\caddy.exe" run --config deploy\Caddyfile
```

Caddy serves:

- `http://192.168.137.1/`
- `/static/*` from `staticfiles/`
- `/media/*` from `media/`

### 5. Allow the firewall port

Allow port 80 on private networks only. Keep port 8000 local to the laptop.

```powershell
New-NetFirewallRule -DisplayName "SBC Attendance HTTP" -Direction Inbound -Protocol TCP -LocalPort 80 -Action Allow -Profile Private
```

If port 80 is already in use, change the Caddyfile to another port, update `CSRF_TRUSTED_ORIGINS` to include that origin, and regenerate QR codes with the new URL.

### 6. Verify from a professor phone

Connect the phone to the laptop hotspot and open:

```text
http://192.168.137.1/check-ip/
```

It should show the phone's `192.168.137.x` address and `on_school_network: true`.

## Daily attendance workflow

1. Start the hotspot, Waitress, and Caddy.
2. Verify `/check-ip/` from a professor phone.
3. In the HR dashboard, click **Generate All QR Codes**.
4. Distribute or print that day's QR codes.
5. Test one time-in and one time-out.
6. Run attendance.
7. Check the records on the laptop.
8. Back up PostgreSQL.

QR codes contain the current date and expire daily. Do not reuse yesterday's QR codes.

## Database backup

Before real attendance data is entered, take a PostgreSQL backup:

```powershell
pg_dump -h 127.0.0.1 -U postgres -Fc -f backups\sbc_attendance_YYYYMMDD.dump sbc_attendance
```

Keep backups outside the repository.

## Tests

```powershell
& ..\myenv\Scripts\python.exe manage.py check
& ..\myenv\Scripts\python.exe manage.py makemigrations --check --dry-run
& ..\myenv\Scripts\python.exe manage.py test
```

`manage.py check --deploy` will warn about HTTP-related settings. Those warnings are expected for this pilot.

## Security notes

- The app uses HTTP for the pilot. Traffic on the hotspot is not encrypted.
- Use a strong hotspot password and only allow known devices.
- Do not port-forward the app to the internet.
- The old `test_sms.py` contained exposed Movider credentials. Rotate those credentials immediately.
- Keep `.env` out of version control.
- Generated QR images are under `media/` and are intentionally ignored by git.

### Removing the leaked file from history

Rotate the Movider credentials first. Then, from the repository that contains `test_sms.py`:

```powershell
git filter-repo --path test_sms.py --invert-paths
```

Coordinate with every clone before force-pushing. This rewrites history and must not be done casually.

## Remote access

Remote access is not supported by the laptop-hotspot pilot. It requires a school-managed server and HTTPS certificate or an approved VPN.
