import ipaddress
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail
from django.utils import timezone

logger = logging.getLogger(__name__)


def get_client_ip(request):
    if getattr(settings, 'TRUST_X_FORWARDED_FOR', False):
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            return x_forwarded_for.split(',')[0].strip()
    return request.META.get('REMOTE_ADDR', '').strip()


def _ip_matches_allowed_range(ip, allowed_ranges):
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for entry in allowed_ranges:
        entry = str(entry).strip()
        if not entry:
            continue

        if '/' in entry:
            try:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                logger.warning("Invalid ALLOWED_IP_RANGES CIDR entry: %s", entry)
            continue

        if entry.endswith('.'):
            if ip.startswith(entry):
                return True
            continue

        try:
            if address == ipaddress.ip_address(entry):
                return True
        except ValueError:
            logger.warning("Invalid ALLOWED_IP_RANGES entry: %s", entry)

    return False


def is_on_school_network(request):
    if getattr(settings, 'WIFI_BYPASS', False):
        logger.warning("WIFI_BYPASS is enabled; allowing attendance from all IP addresses.")
        return True

    ip = get_client_ip(request)
    allowed_ranges = getattr(settings, 'ALLOWED_IP_RANGES', [])
    allowed = _ip_matches_allowed_range(ip, allowed_ranges)
    logger.info("Request IP: %s | school network: %s", ip, allowed)
    return allowed


def send_hr_notification(prof_name, event_message):
    api_key    = settings.MOVIDER_API_KEY.strip()
    api_secret = settings.MOVIDER_API_SECRET.strip()
    to_number  = settings.HR_PHONE_NUMBER.replace('+', '').strip()

    # ✅ Manila time
    local_now = timezone.localtime(timezone.now())
    timestamp = local_now.strftime('%B %d, %Y %I:%M %p')

    sms_text = (
        f"SBC Attendance Alert\n"
        f"Professor: {prof_name}\n"
        f"Status: {event_message}\n"
        f"Time: {timestamp}"
    )

    payload = {
        'api_key':    api_key,
        'api_secret': api_secret,
        'to':         to_number,
        'text':       sms_text,
    }

    # ✅ Show full SMS content in terminal
    print(f"\n{'═' * 50}")
    print(f"📱 SMS NOTIFICATION")
    print(f"{'─' * 50}")
    print(f"  To      : +{to_number}")
    print(f"  Content :")
    for line in sms_text.split('\n'):
        print(f"    {line}")
    print(f"{'═' * 50}")

    try:
        response = requests.post(
            'https://api.movider.co/v1/sms',
            data=payload,
            timeout=30
        )
        result = response.json() if response.headers.get('Content-Type') == 'application/json' else response.text

        if response.status_code == 200:
            logger.info(f"Movider SMS sent successfully: {result}")
            print(f"  ✅ SMS SENT SUCCESSFULLY\n")
            return True
        else:
            logger.error(f"Movider SMS failed: {result}")
            print(f"  ❌ SMS FAILED: {result}\n")
            return False

    except requests.exceptions.Timeout:
        logger.error("Movider request timeout")
        print(f"  ❌ SMS TIMEOUT\n")
        return False
    except requests.exceptions.ConnectionError:
        logger.error("Movider connection error")
        print(f"  ❌ SMS CONNECTION ERROR\n")
        return False
    except Exception as e:
        logger.error(f"Movider unexpected error: {e}")
        print(f"  ❌ SMS ERROR: {e}\n")
        return False


def send_hr_email(prof_name, event_message, request=None):
    """Send HR email alert via Gmail SMTP with detailed info."""

    # ✅ Manila time
    local_now = timezone.localtime(timezone.now())
    date_only = local_now.strftime('%B %d, %Y')
    time_only = local_now.strftime('%I:%M %p')

    # Capture IP using the same trust rules as attendance checks.
    ip_address = (get_client_ip(request) or 'N/A') if request else 'N/A'

    subject = f"📌 SBC Attendance Alert — {prof_name}"
    body = (
        f"📌 Attendance Notification\n"
        f"{'─' * 40}\n\n"
        f"Professor : {prof_name}\n"
        f"Status    : {event_message}\n"
        f"Date      : {date_only}\n"
        f"Time      : {time_only}\n"
        f"IP Address: {ip_address}\n\n"
        f"{'─' * 40}\n"
        f"⚠️  This is an automated alert from the\n"
        f"    SBC Batangas Part-Time Attendance System.\n"
        f"    Do not reply to this email."
    )

    # ✅ Show full email content in terminal
    print(f"\n{'═' * 50}")
    print(f"📧 EMAIL NOTIFICATION")
    print(f"{'─' * 50}")
    print(f"  From    : {settings.EMAIL_HOST_USER}")
    print(f"  To      : {settings.HR_EMAIL}")
    print(f"  Subject : {subject}")
    print(f"  Body    :")
    for line in body.split('\n'):
        print(f"    {line}")
    print(f"{'═' * 50}")

    try:
        send_mail(
            subject,
            body,
            settings.EMAIL_HOST_USER,
            [settings.HR_EMAIL],
            fail_silently=False,
        )
        logger.info("✅ HR Email sent successfully")
        print(f"  ✅ EMAIL SENT SUCCESSFULLY\n")
        return True
    except Exception as e:
        logger.error(f"❌ HR Email failed: {e}")
        print(f"  ❌ EMAIL FAILED: {e}\n")
        return False