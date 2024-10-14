import os
import tempfile
import logging
import re
import zipfile
import smtplib
import random
import time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from dotenv import load_dotenv
from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing
import streamlit as st
from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
import yt_dlp
from pydub import AudioSegment

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Load environment variables
load_dotenv()

# Check for required environment variables
api_key = os.getenv('YOUTUBE_API_KEY')
sender_email = os.getenv('SENDER_EMAIL')
email_password = os.getenv('EMAIL_PASSWORD')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET')
REDIRECT_URI = os.getenv('REDIRECT_URI')  # Should be set to your deployed app's URL + '/callback'

if not all([api_key, sender_email, email_password, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, REDIRECT_URI]):
    st.error("Missing environment variables. Please check your .env file.")
    st.stop()

num_cores = multiprocessing.cpu_count()

# List of common user agents
USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Safari/605.1.15',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36',
    'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.101 Safari/537.36'
]

def get_random_user_agent():
    return random.choice(USER_AGENTS)

def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

def get_youtube_service():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=['https://www.googleapis.com/auth/youtube.force-ssl'],  # Added comma here
        redirect_uri=REDIRECT_URI
    )
    
    # Check if we have stored credentials
    if 'credentials' not in st.session_state:
        authorization_url, _ = flow.authorization_url(prompt='consent')
        st.markdown(f"Please [click here]({authorization_url}) to authorize the application.")
        st.stop()
    
    credentials = Credentials(**st.session_state.credentials)
    return build('youtube', 'v3', credentials=credentials)

def handle_oauth_callback():
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=['https://www.googleapis.com/auth/youtube.force-ssl'],
        redirect_uri=REDIRECT_URI
    )
    
    flow.fetch_token(code=st.experimental_get_query_params()['code'][0])
    credentials = flow.credentials
    st.session_state.credentials = {
        'token': credentials.token,
        'refresh_token': credentials.refresh_token,
        'token_uri': credentials.token_uri,
        'client_id': credentials.client_id,
        'client_secret': credentials.client_secret,
        'scopes': credentials.scopes
    }
    st.experimental_rerun()

@st.cache_data
def get_youtube_links(query, max_results=20):
    try:
        youtube = get_youtube_service()
        search_response = youtube.search().list(
            q=query,
            part='snippet',
            type='video',
            maxResults=max_results
        ).execute()

        videos = []
        for item in search_response['items']:
            video_id = item['id']['videoId']
            video_title = item['snippet']['title']
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            videos.append((video_title, video_url))

        return videos
    except Exception as e:
        logging.error(f"Failed to fetch YouTube links: {e}")
        st.error(f"Failed to fetch YouTube links: {str(e)}")
        return []

def download_single_audio(url, index, download_path):
    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': f'{download_path}/song_{index}.%(ext)s',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
        'retries': 3,
        'fragment_retries': 3,
        'user_agent': get_random_user_agent(),
    }

    max_attempts = 5
    for attempt in range(max_attempts):
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
            downloaded_files = [f for f in os.listdir(download_path) if f.startswith(f"song_{index}.") and f.endswith(".mp3")]
            if downloaded_files:
                return os.path.join(download_path, downloaded_files[0])
            else:
                logging.error(f"Downloaded file not found for {url}")
                return None
        except Exception as e:
            if "Sign in to confirm you're not a bot" in str(e):
                sleep_time = (2 ** attempt) + random.uniform(0, 1)  # Exponential backoff
                logging.info(f"Detected anti-bot measure. Waiting for {sleep_time:.2f} seconds before retrying...")
                time.sleep(sleep_time)
                ydl_opts['user_agent'] = get_random_user_agent()  # Rotate user agent for each attempt
            else:
                logging.error(f"Error downloading audio (attempt {attempt + 1}/{max_attempts}): {e}")
                return None

    logging.error(f"Failed to download audio after {max_attempts} attempts: {url}")
    return None

def download_all_audio(video_urls, download_path):
    downloaded_files = []
    random.shuffle(video_urls)  # Randomize download order
    with ThreadPoolExecutor(max_workers=1) as executor:  # Reduced to 1 worker
        futures = {
            executor.submit(download_single_audio, url, index, download_path): index
            for index, url in enumerate(video_urls, start=1)
        }

        for future in as_completed(futures):
            try:
                mp3_file = future.result()
                if mp3_file:
                    downloaded_files.append(mp3_file)
                time.sleep(random.uniform(5, 10))  # Increased wait time between downloads
            except Exception as e:
                logging.error(f"Error occurred: {e}")

    return downloaded_files

def create_mashup(audio_files, output_file, trim_duration):
    mashup = AudioSegment.silent(duration=0)
    total_trim_duration_per_file = trim_duration * 1000

    for file in audio_files:
        try:
            audio = AudioSegment.from_file(file)
            if len(audio) < total_trim_duration_per_file:
                logging.warning(f"Audio file {file} is shorter than trim duration. Using full length.")
                part = audio
            else:
                start_point = random.randint(0, len(audio) - total_trim_duration_per_file)
                part = audio[start_point:start_point + total_trim_duration_per_file]
            mashup += part
        except Exception as e:
            logging.error(f"Error processing file {file}: {e}")

    if len(mashup) == 0:
        logging.error("No audio files were successfully processed.")
        return None

    expected_mashup_duration = total_trim_duration_per_file * len(audio_files)
    if len(mashup) < expected_mashup_duration:
        logging.warning(f"Mashup duration ({len(mashup)}ms) is less than expected ({expected_mashup_duration}ms).")
    else:
        mashup = mashup[:expected_mashup_duration]

    mashup.export(output_file, format="mp3", bitrate="128k")
    return output_file

def create_zip_file(file_path, zip_path):
    with zipfile.ZipFile(zip_path, 'w') as zipf:
        zipf.write(file_path, os.path.basename(file_path))
    return zip_path

def send_email(sender_email, receiver_email, subject, body, attachment_path, password):
    try:
        msg = MIMEMultipart()
        msg['From'] = sender_email
        msg['To'] = receiver_email
        msg['Subject'] = subject

        msg.attach(MIMEText(body, 'plain'))

        with open(attachment_path, 'rb') as attachment:
            part = MIMEBase('application', 'zip')
            part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header('Content-Disposition', f"attachment; filename= {os.path.basename(attachment_path)}")
            msg.attach(part)

        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(sender_email, password)
        text = msg.as_string()
        server.sendmail(sender_email, receiver_email, text)
        server.quit()

        logging.info("Email sent successfully!")
        return True
    except Exception as e:
        logging.error(f"Failed to send email: {e}")
        st.error(f"Failed to send email: {str(e)}")
        return False

def main():
    if 'code' in st.experimental_get_query_params():
        handle_oauth_callback()
        return

    st.title("YouTube Mashup Creator")

    singer_name = st.text_input("Enter singer name:")
    num_videos = st.slider("Number of videos to use (min 10):", min_value=10, max_value=50, value=20)
    trim_duration = st.slider("Duration of each clip (in seconds, min 20):", min_value=20, max_value=60, value=30)
    receiver_email = st.text_input("Enter your email address:")

    if st.button("Create Mashup"):
        if not singer_name or not is_valid_email(receiver_email):
            st.error("Please enter a valid singer name and email address.")
            return

        try:
            with st.spinner("Creating mashup..."):
                videos = get_youtube_links(singer_name, max_results=num_videos)

                if not videos:
                    st.error(f"No videos found for {singer_name}. Please try a different singer name.")
                    return

                download_path = tempfile.mkdtemp()
                video_urls = [url for _, url in videos]
                audio_files = download_all_audio(video_urls, download_path)

                if not audio_files:
                    st.error("Failed to download audio files. Please try again.")
                    return

                output_file = os.path.join(tempfile.gettempdir(), "mashup.mp3")
                mashup_file = create_mashup(audio_files, output_file, trim_duration)

                if not mashup_file:
                    st.error("Failed to create mashup. Please try again.")
                    return

                zip_file = os.path.join(tempfile.gettempdir(), "mashup.zip")
                create_zip_file(mashup_file, zip_file)

                subject = f"Your {singer_name} YouTube Mashup"
                body = f"Please find attached your custom YouTube mashup of {singer_name} songs. Duration: {trim_duration * len(audio_files)} seconds."
                email_sent = send_email(sender_email, receiver_email, subject, body, zip_file, email_password)

                os.remove(mashup_file)
                os.remove(zip_file)
                for file in audio_files:
                    os.remove(file)

                if email_sent:
                    st.success("Mashup created and sent successfully! Check your email.")
                else:
                    st.error("Mashup created but failed to send email. Please try again.")
        except Exception as e:
            logging.error(f"An error occurred: {str(e)}")
            st.error(f"An error occurred: {str(e)}")

if __name__ == '__main__':
    main()
