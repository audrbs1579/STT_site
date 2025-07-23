import azure.functions as func
import logging
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
import requests
import os
import time
import json
from datetime import datetime, timedelta
import uuid
import io
import torchaudio  # MP3 to WAV 변환

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="UploadAndTranscribe", methods=["POST"])
def upload_and_transcribe(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger processed a request.')
    try:
        file = req.files.get('file')
        if not file:
            return func.HttpResponse(json.dumps({"error": "No file uploaded."}), status_code=400, mimetype="application/json")

        # 파일 메모리에 로드
        audio_data = io.BytesIO(file.stream.read())

        # torchaudio로 로드 및 WAV 변환 (mono, 16kHz)
        waveform, sample_rate = torchaudio.load(audio_data)
        waveform = torchaudio.transforms.Resample(sample_rate, 16000)(waveform)
        if waveform.size(0) > 1:  # stereo to mono
            waveform = waveform.mean(dim=0, keepdim=True)
        wav_buffer = io.BytesIO()
        torchaudio.save(wav_buffer, waveform, 16000, format="WAV")
        wav_buffer.seek(0)

        conn_str = os.environ['STORAGE_CONNECTION_STRING']
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client('audio-files')
        if not container_client.exists():
            container_client.create_container()

        safe_filename = str(uuid.uuid4()) + ".wav"  # WAV로 업로드
        blob_client = container_client.get_blob_client(safe_filename)
        logging.info(f"Uploading converted file: {safe_filename}")
        blob_client.upload_blob(wav_buffer, overwrite=True)

        sas_token = generate_blob_sas(
            account_name=blob_service.account_name,
            container_name=container_client.container_name,
            blob_name=blob_client.blob_name,
            account_key=blob_service.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=1)
        )
        sas_url = f"{blob_client.url}?{sas_token}"
        logging.info(f"SAS URL: {sas_url}")

        speech_key = os.environ['SPEECH_KEY']
        speech_region = os.environ['SPEECH_REGION']
        endpoint = f"https://{speech_region}.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions"
        headers = {
            'Ocp-Apim-Subscription-Key': speech_key,
            'Content-Type': 'application/json'
        }
        body = {
            "contentUrls": [sas_url],
            "locale": "ko-KR",
            "displayName": "MP3 Transcription",
            "properties": {"wordLevelTimestampsEnabled": True}
        }
        logging.info("Calling Speech API...")
        response = requests.post(endpoint, headers=headers, json=body)
        logging.info(f"Speech POST status: {response.status_code}")
        if response.status_code != 201:
            error_msg = response.text if response.text else "Unknown transcription error"
            logging.error(f"Speech error: {error_msg}")
            return func.HttpResponse(json.dumps({"error": error_msg}), status_code=500, mimetype="application/json")

        transcription_url = response.headers['Location']
        logging.info(f"Transcription URL: {transcription_url}")
        poll_count = 0
        while poll_count < 30:
            status_res = requests.get(transcription_url, headers=headers)
            logging.info(f"Polling status: {status_res.status_code}")
            if status_res.status_code != 200:
                logging.error(f"Polling error: {status_res.text}")
                return func.HttpResponse(json.dumps({"error": status_res.text}), status_code=500, mimetype="application/json")

            status_data = status_res.json()
            status = status_data.get('status')
            logging.info(f"Status: {status}")
            if status == 'Succeeded':
                files_url = status_data['links']['files']
                files_res = requests.get(files_url, headers=headers)
                content_url = files_res.json()['values'][0]['links']['contentUrl']
                content_res = requests.get(content_url)
                transcription = content_res.json()
                return func.HttpResponse(json.dumps({"transcription": transcription}), status_code=200, mimetype="application/json")
            elif status == 'Failed':
                error_msg = status_data.get('properties', {}).get('error', {}).get('message', 'Transcription failed')
                logging.error(f"Failed: {error_msg}")
                return func.HttpResponse(json.dumps({"error": error_msg}), status_code=500, mimetype="application/json")
            time.sleep(10)
            poll_count += 1

        return func.HttpResponse(json.dumps({"error": "Transcription timeout"}), status_code=500, mimetype="application/json")

    except Exception as e:
        logging.error(f"Exception: {str(e)}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")