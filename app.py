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

if not all([api_key, sender_email, email_password]):
    st.error("Missing environment variables. Please check your .env file.")
    st.stop()

num_cores = multiprocessing.cpu_count()

def is_valid_email(email):
    pattern = r'^[\w\.-]+@[\w\.-]+\.\w+$'
    return re.match(pattern, email) is not None

@st.cache_data
def get_youtube_links(api_key, query, max_results=20):
    try:
        youtube = build('youtube', 'v3', developerKey=api_key)
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
    }

    max_attempts = 5
    base_delay = 5
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
            logging.error(f"Error downloading audio (attempt {attempt + 1}/{max_attempts}): {e}")
            if "Sign in to confirm you're not a bot" in str(e):
                delay = base_delay * (2 ** attempt) + random.uniform(0, 1)
                logging.info(f"Detected anti-bot measure. Waiting for {delay:.2f} seconds before retrying...")
                time.sleep(delay)
            else:
                return None 
    
    logging.error(f"Failed to download audio after {max_attempts} attempts: {url}")
    return None

def download_all_audio(video_urls, download_path):
    downloaded_files = []
    max_workers = min(num_cores, 2)  # Reduced concurrent downloads
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(download_single_audio, url, index, download_path): index
            for index, url in enumerate(video_urls, start=1)
        }

        for future in as_completed(futures):
            try:
                mp3_file = future.result()
                if mp3_file:
                    downloaded_files.append(mp3_file)
                time.sleep(random.uniform(3, 7))  # Increased delay between downloads
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
                part = audio[:total_trim_duration_per_file]
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
                videos = get_youtube_links(api_key, singer_name, max_results=num_videos)

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
