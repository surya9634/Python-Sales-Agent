import os
import time
import logging
import requests
import webbrowser
import json
import sys
from flask import Flask, request, jsonify, redirect
from facebook_business.api import FacebookAdsApi
from facebook_business.adobjects.iguser import IGUser
from facebook_business.exceptions import FacebookRequestError
from groq import Groq
from pyngrok import ngrok, conf
from dotenv import load_dotenv
import threading

# Load environment variables
load_dotenv()
app = Flask(__name__)

# Logging setup
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', stream=sys.stdout)
logger = logging.getLogger('InstagramBot')

# Configuration
NGROK_AUTH_TOKEN = os.getenv('NGROK_AUTH_TOKEN')
FACEBOOK_APP_ID = os.getenv('FACEBOOK_APP_ID')
FACEBOOK_APP_SECRET = os.getenv('FACEBOOK_APP_SECRET')
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
WEBHOOK_VERIFY_TOKEN = os.getenv('WEBHOOK_VERIFY_TOKEN')
REDIRECT_URI_OVERRIDE = os.getenv('REDIRECT_URI_OVERRIDE')

ACCESS_TOKEN = None
INSTAGRAM_BUSINESS_ID = None
PAGE_TOKEN = None
PUBLIC_URL = None

SYSTEM_PROMPT = """
You're a helpful customer support assistant for an Instagram store. 
Respond in 1-2 sentences. Be friendly and professional.
If you can't help, say: "Let me connect you with a human specialist!"
"""

def start_ngrok_tunnel():
    if REDIRECT_URI_OVERRIDE:
        logger.info(f"✅ Using static redirect URI: {REDIRECT_URI_OVERRIDE}")
        return REDIRECT_URI_OVERRIDE.replace('/oauth-callback', '')
    try:
        conf.get_default().auth_token = NGROK_AUTH_TOKEN
        tunnel = ngrok.connect(5000, bind_tls=True)
        return tunnel.public_url
    except Exception as e:
        logger.error(f"Failed to start ngrok tunnel: {str(e)}")
        return None

def get_oauth_url(redirect_uri):
    return (
        "https://www.facebook.com/v19.0/dialog/oauth?"
        f"client_id={FACEBOOK_APP_ID}&"
        f"redirect_uri={redirect_uri}&"
        "scope=instagram_manage_messages,pages_manage_metadata,pages_read_engagement&"
        "response_type=code"
    )

def exchange_code_for_token(code, redirect_uri):
    try:
        response = requests.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                'client_id': FACEBOOK_APP_ID,
                'client_secret': FACEBOOK_APP_SECRET,
                'redirect_uri': redirect_uri,
                'code': code
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json()['access_token']
    except Exception as e:
        logger.error(f"Token exchange failed: {str(e)}")
        return None

def get_long_lived_token(short_lived_token):
    try:
        response = requests.get(
            "https://graph.facebook.com/v19.0/oauth/access_token",
            params={
                'grant_type': 'fb_exchange_token',
                'client_id': FACEBOOK_APP_ID,
                'client_secret': FACEBOOK_APP_SECRET,
                'fb_exchange_token': short_lived_token
            },
            timeout=10
        )
        response.raise_for_status()
        return response.json()['access_token']
    except Exception as e:
        logger.error(f"Long-lived token exchange failed: {str(e)}")
        return None

def get_page_and_ig_ids(access_token):
    try:
        response = requests.get(
            "https://graph.facebook.com/v19.0/me/accounts",
            params={'access_token': access_token},
            timeout=10
        )
        response.raise_for_status()
        pages_data = response.json()
        if not pages_data.get('data'):
            logger.error("No Facebook pages found for this user")
            return None, None

        page = pages_data['data'][0]
        page_id = page['id']
        page_token = page['access_token']

        ig_response = requests.get(
            f"https://graph.facebook.com/v19.0/{page_id}",
            params={
                'access_token': page_token,
                'fields': 'instagram_business_account'
            },
            timeout=10
        )
        ig_response.raise_for_status()
        ig_data = ig_response.json()
        ig_business_id = ig_data.get('instagram_business_account', {}).get('id')
        if not ig_business_id:
            logger.error("No Instagram Business Account connected to this page")
            return None, None

        return page_token, ig_business_id
    except Exception as e:
        logger.error(f"Error getting IDs: {str(e)}")
        return None, None

def register_webhook(ig_business_id, page_token, webhook_url):
    try:
        response = requests.post(
            f"https://graph.facebook.com/v19.0/{ig_business_id}/subscribed_apps",
            params={
                'access_token': page_token,
                'subscribed_fields': 'messages'
            },
            timeout=10
        )
        response.raise_for_status()
        if response.json().get('success'):
            logger.info(f"✅ Webhook registered for IG Business ID: {ig_business_id}")
            return True
        logger.error(f"❌ Webhook registration failed: {response.text}")
        return False
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return False

@app.route('/')
def home():
    global ACCESS_TOKEN, INSTAGRAM_BUSINESS_ID, PUBLIC_URL
    if ACCESS_TOKEN:
        return f"""
        <h1>Instagram AI Bot</h1>
        <p>Status: <span style="color:green">Authenticated</span></p>
        <p>Instagram Business ID: {INSTAGRAM_BUSINESS_ID}</p>
        <p><a href="/test">Send Test Message</a></p>
        """
    redirect_uri = f"{PUBLIC_URL}/oauth-callback"
    auth_url = get_oauth_url(redirect_uri)
    return redirect(auth_url)

@app.route('/oauth-callback')
def oauth_callback():
    global ACCESS_TOKEN, INSTAGRAM_BUSINESS_ID, PAGE_TOKEN, PUBLIC_URL

    code = request.args.get('code')
    error = request.args.get('error')
    redirect_uri = f"{PUBLIC_URL}/oauth-callback"

    if error:
        return f"Authorization failed: {error}", 400
    if not code:
        return "Authorization failed: No code returned", 400

    short_lived_token = exchange_code_for_token(code, redirect_uri)
    if not short_lived_token:
        return "Token exchange failed", 400

    long_lived_token = get_long_lived_token(short_lived_token)
    if not long_lived_token:
        return "Long-lived token exchange failed", 400

    page_token, ig_business_id = get_page_and_ig_ids(long_lived_token)
    if not page_token or not ig_business_id:
        return "Failed to get page and Instagram info", 400

    ACCESS_TOKEN = long_lived_token
    PAGE_TOKEN = page_token
    INSTAGRAM_BUSINESS_ID = ig_business_id

    try:
        FacebookAdsApi.init(access_token=PAGE_TOKEN)
    except Exception as e:
        logger.error(f"Facebook API init failed: {str(e)}")

    webhook_url = f"{PUBLIC_URL}/webhook"
    if register_webhook(ig_business_id, page_token, webhook_url):
        return redirect('/')
    return "Webhook registration failed", 400

@app.route('/test')
def test_page():
    return "<h1>Test Page</h1><p>Status: Running</p>"

@app.route('/webhook', methods=['GET'])
def verify_webhook():
    if request.args.get('hub.verify_token') == WEBHOOK_VERIFY_TOKEN:
        return request.args.get('hub.challenge'), 200
    return "Invalid token", 403

@app.route('/webhook', methods=['POST'])
def handle_messages():
    try:
        data = request.json
        logger.info(f"Webhook payload received")

        with open('webhook_log.json', 'a') as log_file:
            json.dump(data, log_file, indent=2)
            log_file.write('\n')

        for entry in data.get('entry', []):
            for event in entry.get('messaging', []):
                if 'message' in event:
                    process_message(event)
        return jsonify(status="success"), 200
    except Exception as e:
        logger.error(f"Webhook processing error: {e}")
        return jsonify(status="error", message=str(e)), 500

def process_message(event):
    global PAGE_TOKEN, GROQ_API_KEY
    try:
        sender_id = event['sender']['id']
        message = event['message'].get('text', '').strip()

        if not message:
            logger.info(f"Ignoring empty message from {sender_id}")
            return

        groq_client = Groq(api_key=GROQ_API_KEY)
        response = groq_client.chat.completions.create(
            model="llama3-70b-8192",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": message}
            ],
            temperature=0.7,
            max_tokens=150
        )
        ai_response = response.choices[0].message.content.strip()

        ig_user = IGUser(sender_id)
        ig_user.create_message(message=ai_response, messaging_type="RESPONSE")
        logger.info(f"Responded: {ai_response}")
    except FacebookRequestError as e:
        if e.api_error_code() == 613:
            time.sleep(300)
            process_message(event)
        else:
            logger.error(f"Facebook error: {e.api_error_message()}")
    except Exception as e:
        logger.error(f"Message processing error: {e}")

def open_browser(url):
    time.sleep(2)
    try:
        webbrowser.open(url)
    except Exception:
        print(f"Open this URL manually: {url}")

def manual_configuration_required(redirect_uri):
    print("\n" + "="*80)
    print("⚠️ ACTION REQUIRED: Add the following URI to Facebook Developer Console")
    print(f"👉 URI to whitelist: {redirect_uri}")
    print("1. Visit: https://developers.facebook.com/apps")
    print("2. Select your App > Facebook Login > Settings")
    print("3. Paste the URI into 'Valid OAuth Redirect URIs'")
    print("4. Save changes, then restart this app.")
    print("="*80 + "\n")

if __name__ == '__main__':
    missing = [v for v in ['NGROK_AUTH_TOKEN', 'FACEBOOK_APP_ID', 'FACEBOOK_APP_SECRET', 'GROQ_API_KEY', 'WEBHOOK_VERIFY_TOKEN'] if not os.getenv(v)]
    if missing:
        logger.critical("Missing in .env: " + ', '.join(missing))
        exit(1)

    PUBLIC_URL = start_ngrok_tunnel()
    if not PUBLIC_URL:
        logger.critical("❌ Unable to start ngrok or override URL")
        exit(1)

    redirect_uri = f"{PUBLIC_URL}/oauth-callback"
    manual_configuration_required(redirect_uri)

    threading.Thread(target=open_browser, args=(PUBLIC_URL,)).start()

    try:
        app.run(port=5000)
    except Exception as e:
        logger.critical(f"Server startup error: {str(e)}")
        exit(1)
