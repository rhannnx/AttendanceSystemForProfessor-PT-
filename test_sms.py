import requests

response = requests.post(
    'https://api.movider.co/v1/sms',
    auth=('3DbdAzWMIj7xQIzYGBZh8BFzTyY', 'FPNs-7qFr0I3VgRCW9Me8BPNFScKSX'),
    data={
        'to': '639198256549',
        'text': 'Test from Django SBC Attendance',
    }
)
print(f"Status: {response.status_code}")
print(f"Response: {response.json()}")