import requests
from django.conf import settings
from django.utils import timezone
import logging
from django.core.mail import send_mail

logger = logging.getLogger(__name__)

ALLOWED_IP_RANGES = [
    '127.0.0.1',
    '192.168.64.',
    '192.168.65.',
    '192.168.66.',
    '192.168.67.',
    '192.168.68.',
    '192.168.69.',
    '192.168.70.',
    '192.168.71.',
    '192.168.72.',
    '192.168.73.',
    '192.168.74.',
    '192.168.75.',
    '192.168.76.',
    '192.168.77.',
    '192.168.78.',
    '192.168.79.',
]

def is_on_school_network(request):
    x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
    if x_forwarded_for:
        ip = x_forwarded_for.split(',')[0].strip()
    else:
        ip = request.META.get('REMOTE_ADDR', '')
    logger.info(f"Request IP: {ip}")
    return any(ip.startswith(allowed) for allowed in ALLOWED_IP_RANGES)


def send_hr_notification(prof_name, event_message):
    print(f"\n🔔 Attempting SMS for: {prof_name}")

    api_key = settings.MOVIDER_API_KEY.strip()
    api_secret = settings.MOVIDER_API_SECRET.strip()
    to_number = settings.HR_PHONE_NUMBER.replace('+', '').strip()

    timestamp = timezone.now().strftime('%Y-%m-%d %H:%M')

    sms_message = (
        f"SBC Attendance Alert\n"
        f"Professor: {prof_name}\n"
        f"Status: {event_message}\n"
        f"Time: {timestamp}"
    )

    payload = {
    'api_key': api_key,
    'api_secret': api_secret,
    'to': to_number,
    'text': f"SBC Attendance Alert\nProfessor: {prof_name}\nStatus: {event_message}\nTime: {timestamp}"
    }


    try:
        response = requests.post(
            'https://api.movider.co/v1/sms',
            data=payload,
            timeout=30
        )
        result = response.json() if response.headers.get('Content-Type') == 'application/json' else response.text

        if response.status_code == 200:
            logger.info(f"Movider SMS sent successfully: {result}")
            print("✅ SMS SENT SUCCESSFULLY")
            return True
        else:
            logger.error(f"Movider SMS failed: {result}")
            print("❌ SMS FAILED")
            return False

    except requests.exceptions.Timeout:
        logger.error("Movider request timeout")
        print("❌ REQUEST TIMEOUT")
        return False
    except requests.exceptions.ConnectionError:
        logger.error("Movider connection error")
        print("❌ CONNECTION ERROR")
        return False
    except Exception as e:
        logger.error(f"Movider unexpected error: {e}")
        print(f"❌ UNEXPECTED ERROR: {e}")
        return False


def send_hr_email(prof_name, event_message, request=None):
    """Send HR email alert via Gmail SMTP with detailed info."""
    timestamp = timezone.now().strftime('%Y-%m-%d %H:%M')
    
    # Capture IP if request is passed
    ip_address = None
    if request:
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip_address = x_forwarded_for.split(',')[0].strip()
        else:
            ip_address = request.META.get('REMOTE_ADDR', '')

    subject = f"SBC Attendance Alert - {event_message}"
    body = (
        f"📌 Attendance Notification\n\n"
        f"Professor: {prof_name}\n"
        f"Status: {event_message}\n"
        f"Time: {timestamp}\n"
        f"IP Address: {ip_address or 'N/A'}\n\n"
        f"⚠️ This is an automated alert from the SBC Attendance System."
    )

    try:
        send_mail(
            subject,
            body,
            settings.EMAIL_HOST_USER,          # sender Gmail
            [settings.HR_EMAIL],               # HR recipient
            fail_silently=False,
        )
        logger.info("✅ HR Email sent successfully")
        print("✅ EMAIL SENT SUCCESSFULLY")
        return True
    except Exception as e:
        logger.error(f"❌ HR Email failed: {e}")
        print(f"❌ EMAIL FAILED: {e}")
        return False
