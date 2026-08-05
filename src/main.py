from  google.oauth2 import service_account
import json
import requests
import os


def get_gcp_credentials():
    service_account_json = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    if not service_account_json:
        return None
        # Load the service account info from the JSON string
    
    service_account_info = json.loads(service_account_json)
    
    # Create a credentials object from the service account info
    credentials = service_account.Credentials.from_service_account_info(service_account_info)
    return credentials

def send_notification(title, message):
    

    # Get the NTFY topic from environment variable
    ntfy_topic = os.getenv("NTFY_TOPIC")
    if not ntfy_topic:
        raise ValueError("NTFY_TOPIC environment variable is not set.")

    # Construct the NTFY URL
    ntfy_url = f"https://ntfy.sh/{ntfy_topic}"

    # Prepare the payload
    payload = {
        "title": title,
        "message": message
    }

    # Send the notification
    response = requests.post(ntfy_url, json=payload)

    if response.status_code == 200:
        print("Notification sent successfully.")
    else:
        print(f"Failed to send notification. Status code: {response.status_code}, Response: {response.text}")


def main():
    # Example usage
    title = "Job Search Update"
    message = "New job listings are available!"
    send_notification(title, message)

if __name__ == "__main__":
    main()