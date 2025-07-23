import azure.functions as func
import logging
from azure.storage.blob import BlobServiceClient
import requests
import os
import time
import json
from datetime import datetime, timedelta, timezone

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="UploadAndTranscribe", methods=["POST"])
def upload_and_transcribe(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger processed a request.')
    try:
        file = req.files.get('file')
        if not file:
            return func.HttpResponse("No file uploaded.", status_code=400)

        # Blob Storage에 MP3 업로드
        conn_str = os.environ['STORAGE_CONNECTION_STRING']
        blob_service = BlobServiceClient.from_connection_string(conn_str)
        container_client = blob_service.get_container_client('audio-files')
        blob_client = container_client.get_blob_client(file.filename)
        blob_client.upload_blob(file.stream.read(), overwrite=True)

        # SAS URL 생성
        sas_token = blob_client.generate_shared_access_signature(
            permission="r", expiry=datetime.now(timezone.utc) + timedelta(hours=1)
        )
        sas_url = f"{blob_client.url}?{sas_token}"

        # Azure AI Speech Batch Transcription 호출
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
        response = requests.post(endpoint, headers=headers, json=body)
        if response.status_code != 201:
            return func.HttpResponse(f"Transcription error: {response.text}", status_code=500)

        transcription_url = response.headers['Location']
        # Polling으로 결과 확인
        while True:
            status_res = requests.get(transcription_url, headers=headers)
            status = status_res.json()['status']
            if status == 'Succeeded':
                files_url = status_res.json()['links']['files']
                files_res = requests.get(files_url, headers=headers)
                content_url = files_res.json()['values'][0]['links']['contentUrl']
                content_res = requests.get(content_url)
                transcription = content_res.json()
                return func.HttpResponse(json.dumps({"transcription": transcription}), status_code=200, mimetype="application/json")
            elif status == 'Failed':
                return func.HttpResponse("Transcription failed.", status_code=500)
            time.sleep(10)

    except Exception as e:
        return func.HttpResponse(f"Error: {str(e)}", status_code=500)