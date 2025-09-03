import base64
import json
import os
import requests
import traceback
from google.cloud import secretmanager
import functions_framework
from flask import Request

# Your Google Cloud Project ID (for Secret Manager)
# You can get rid of this and hard-code the client secret & token bellow if you don't want to use secret manager
GCP_PROJECT_ID = ""

# The device ID of your thermostat. You can get this from an initial device list call.
# You could also get this dynamically from the event if you have multiple thermostats.
THERMOSTAT_DEVICE_ID = ""

# Fan duration settings
FAN_DURATION_SECONDS = "360s"  # 6 minutes

print(f"GCP_PROJECT_ID: {GCP_PROJECT_ID}")

def get_secret(secret_id, version_id="latest"):
    """Fetches a secret from Google Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{GCP_PROJECT_ID}/secrets/{secret_id}/versions/{version_id}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("UTF-8")

def get_new_access_token():
    """Uses the refresh token to get a new access token."""
    client_id = get_secret("nest-client-id")
    client_secret = get_secret("nest-client-secret")
    refresh_token = get_secret("nest-refresh-token")

    response = requests.post(
        "https://www.googleapis.com/oauth2/v4/token",
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
    )
    response.raise_for_status()
    return response.json()["access_token"]

@functions_framework.http
def process_nest_event(request: Request):
    """
    This function is triggered by a Pub/Sub message via Cloud Run using Functions Framework.
    It checks for the end of a heat/cool cycle and turns on the fan for different durations
    based on whether it was a heating or cooling cycle.
    """
    try:
        # Get the request data
        envelope = request.get_json(silent=True)
        if not envelope:
            print("No JSON payload received")
            return "Bad Request: No JSON payload", 400

        # Extract the Pub/Sub message
        if 'message' not in envelope:
            print("No message field in request")
            return "Bad Request: No message field", 400

        pubsub_message = envelope['message']

        # Decode the message data
        if 'data' not in pubsub_message:
            print("No data field in message")
            return "Bad Request: No data field", 400

        # Handle both base64 encoded and plain string data
        message_data = pubsub_message['data']
        try:
            # Try to decode as base64 first
            decoded_data = base64.b64decode(message_data).decode("utf-8")
        except Exception as decode_error:
            # If base64 decode fails, treat as plain string
            print(f"Base64 decode failed: {decode_error}, treating as plain string")
            decoded_data = message_data if isinstance(message_data, str) else str(message_data)

        # Parse JSON data
        try:
            data = json.loads(decoded_data)
        except json.JSONDecodeError as json_error:
            print(f"JSON decode error: {json_error}")
            print(f"Raw data: {decoded_data}")
            return "Bad Request: Invalid JSON", 400

        print(f"Received event: {data}")

        # Check if this is the event we care about
        if "resourceUpdate" not in data:
            print("Not a resource update event. Exiting.")
            return "OK: Event ignored", 204

        traits = data["resourceUpdate"]["traits"]
        
        # IMPORTANT FIX: Only process HVAC trait updates, ignore Fan trait updates
        if "sdm.devices.traits.ThermostatHvac" not in traits:
            print("Not an HVAC trait update. Exiting.")
            return "OK: Event ignored", 204
            
        # Also ignore if this event ONLY contains Fan trait updates
        if "sdm.devices.traits.Fan" in traits and "sdm.devices.traits.ThermostatHvac" not in traits:
            print("Fan-only trait update, ignoring.")
            return "OK: Event ignored", 204

        hvac_update = traits["sdm.devices.traits.ThermostatHvac"]
        
        # The trigger condition: HVAC status has just become "OFF"
        current_status = hvac_update.get("status")
        if current_status == "OFF":
            print("HVAC cycle ended.")

            fan_duration = FAN_DURATION_SECONDS

            # Get a new access token and execute command
            access_token = get_new_access_token()
            project_id = get_secret("nest-project-id")
            url = f"https://smartdevicemanagement.googleapis.com/v1/enterprises/{project_id}/devices/{THERMOSTAT_DEVICE_ID}:executeCommand"

            headers = {"Authorization": f"Bearer {access_token}", "Content-Type": "application/json"}
            payload = {
                "command": "sdm.devices.commands.Fan.SetTimer",
                "params": {
                    "timerMode": "ON",
                    "duration": fan_duration
                }
            }

            response = requests.post(url, headers=headers, json=payload)
            response.raise_for_status()
            print(f"Successfully sent FAN start command. Response: {response.json()}")
            return f"OK: Fan activated for {fan_duration} after cycle", 200
        else:
            print(f"HVAC status is {current_status}, not OFF. No action taken.")
            return "OK: Condition not met", 204

    except Exception as e:
        print(f"An error occurred: {e}")
        traceback.print_exc()
        return "Internal Server Error", 500
