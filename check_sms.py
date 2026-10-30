import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')


def main():
    api_key = os.getenv('MOVIDER_API_KEY', '').strip()
    api_secret = os.getenv('MOVIDER_API_SECRET', '').strip()
    to_number = os.getenv('HR_PHONE_NUMBER', '').replace('+', '').strip()

    if not all([api_key, api_secret, to_number]):
        raise SystemExit(
            'MOVIDER_API_KEY, MOVIDER_API_SECRET, and HR_PHONE_NUMBER must be set in .env'
        )

    response = requests.post(
        'https://api.movider.co/v1/sms',
        auth=(api_key, api_secret),
        data={
            'to': to_number,
            'text': 'Test from Django SBC Attendance',
        },
        timeout=30,
    )
    print(f'Status: {response.status_code}')
    print(f'Response: {response.text}')


if __name__ == '__main__':
    main()
