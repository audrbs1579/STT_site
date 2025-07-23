# 파일 이름: function_app.py
# 설명: FFmpeg 경로 유효성 검사를 추가하여 안정성을 극대화한 최종 버전입니다.

import logging
import os
import json
import time
import uuid
import io
from datetime import datetime, timedelta

import azure.functions as func
from azure.storage.blob import BlobServiceClient, generate_blob_sas, BlobSasPermissions
import requests
from pydub import AudioSegment
import ffmpeg_downloader as ffdl

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="UploadAndTranscribe", methods=["POST"])
def upload_and_transcribe(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function: "UploadAndTranscribe"가 요청을 받았습니다.')

    # ★★★★★ 중요: FFmpeg 경로 검증 및 설정 ★★★★★
    try:
        logging.info("FFmpeg 설정을 시작합니다...")
        ffmpeg_path = ffdl.ffmpeg_path
        ffprobe_path = ffdl.ffprobe_path

        # 1. 경로 변수가 None이 아닌지 확인
        if not ffmpeg_path or not ffprobe_path:
            raise FileNotFoundError("ffmpeg-downloader가 FFmpeg 또는 ffprobe 경로를 반환하지 못했습니다.")

        # 2. 해당 경로에 실제 파일이 존재하는지 확인
        if not os.path.exists(ffmpeg_path):
            raise FileNotFoundError(f"지정된 경로에 FFmpeg 실행 파일이 없습니다: {ffmpeg_path}")
        if not os.path.exists(ffprobe_path):
            raise FileNotFoundError(f"지정된 경로에 ffprobe 실행 파일이 없습니다: {ffprobe_path}")
            
        logging.info(f"FFmpeg 경로 확인: {ffmpeg_path}")
        logging.info(f"ffprobe 경로 확인: {ffprobe_path}")

        # pydub에 FFmpeg 및 ffprobe 경로를 명시적으로 지정
        AudioSegment.converter = ffmpeg_path
        AudioSegment.ffprobe = ffprobe_path
        
        logging.info("pydub 라이브러리에 FFmpeg 및 ffprobe 경로를 성공적으로 설정했습니다.")

    except Exception as ffmpeg_e:
        logging.error(f"FFmpeg 설정 중 심각한 오류 발생: {str(ffmpeg_e)}", exc_info=True)
        return func.HttpResponse(
            json.dumps({"error": f"서버 환경 설정 오류 (FFmpeg): {str(ffmpeg_e)}"}),
            status_code=500,
            mimetype="application/json"
        )

    try:
        # 1. 파일 업로드 확인
        file = req.files.get('file')
        if not file:
            return func.HttpResponse(json.dumps({"error": "요청에 파일이 포함되지 않았습니다."}), status_code=400, mimetype="application/json")

        filename = file.filename
        file_bytes = file.stream.read()
        logging.info(f"파일 수신 완료: {filename}, 크기: {len(file_bytes)} bytes")

        # 2. 오디오 변환 (pydub 사용)
        logging.info("pydub을 사용하여 오디오 변환을 시작합니다...")
        try:
            audio = AudioSegment.from_file(io.BytesIO(file_bytes))
            logging.info("오디오 파일을 성공적으로 로드했습니다.")
            audio = audio.set_frame_rate(16000).set_channels(1)
            logging.info("오디오를 16kHz, 모노 채널로 변환했습니다.")
            wav_buffer = io.BytesIO()
            audio.export(wav_buffer, format="wav")
            wav_buffer.seek(0)
            logging.info("WAV 형식으로 메모리 내 변환을 완료했습니다.")
        except Exception as audio_e:
            logging.error(f"오디오 변환 중 오류 발생: {str(audio_e)}", exc_info=True)
            return func.HttpResponse(
                json.dumps({"error": "오디오 파일을 처리할 수 없습니다. 파일이 손상되었거나 지원되지 않는 오디오 형식일 수 있습니다."}),
                status_code=400,
                mimetype="application/json"
            )

        # 3. Blob Storage에 변환된 WAV 파일 업로드 (이하 로직은 동일)
        conn_str = os.environ['STORAGE_CONNECTION_STRING']
        container_name = 'audio-files'
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        blob_name = f"{str(uuid.uuid4())}.wav"
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        logging.info(f"'{blob_name}' 이름으로 Blob Storage에 업로드를 시작합니다...")
        blob_client.upload_blob(wav_buffer, overwrite=True)
        logging.info("Blob Storage에 업로드 완료.")

        # ... (이하 코드는 이전과 동일) ...

        sas_token = generate_blob_sas(
            account_name=blob_service_client.account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=blob_service_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=1)
        )
        sas_url = f"{blob_client.url}?{sas_token}"
        
        speech_key = os.environ['SPEECH_KEY']
        speech_region = os.environ['SPECH_REGION']
        endpoint = f"https://{speech_region}.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions"

        headers = {'Ocp-Apim-Subscription-Key': speech_key, 'Content-Type': 'application/json'}
        body = {
            "contentUrls": [sas_url], "locale": "ko-KR", "displayName": "My Transcription Task",
            "properties": {"wordLevelTimestampsEnabled": True, "diarizationEnabled": True}
        }

        response = requests.post(endpoint, headers=headers, json=body)
        
        if response.status_code != 201:
            error_msg = response.text
            logging.error(f"Speech API 요청 실패. 상태 코드: {response.status_code}, 메시지: {error_msg}")
            return func.HttpResponse(json.dumps({"error": f"Speech API 오류: {error_msg}"}), status_code=500, mimetype="application/json")
        
        transcription_url = response.headers['Location']
        
        poll_count = 0
        while poll_count < 30:
            time.sleep(10)
            status_res = requests.get(transcription_url, headers=headers)
            status_data = status_res.json()
            status = status_data.get('status')
            logging.info(f"현재 변환 상태: {status}")

            if status == 'Succeeded':
                files_url = status_data['links']['files']
                files_res = requests.get(files_url, headers=headers)
                content_url = files_res.json()['values'][0]['links']['contentUrl']
                content_res = requests.get(content_url)
                transcription_result = content_res.json()
                return func.HttpResponse(json.dumps(transcription_result), status_code=200, mimetype="application/json")
            
            elif status == 'Failed':
                error_info = status_data.get('properties', {}).get('error', {})
                error_msg = error_info.get('message', '알 수 없는 변환 실패')
                return func.HttpResponse(json.dumps({"error": error_msg}), status_code=500, mimetype="application/json")
            
            poll_count += 1
        
        return func.HttpResponse(json.dumps({"error": "Transcription timed out after 5 minutes."}), status_code=500, mimetype="application/json")

    except Exception as e:
        logging.error(f"처리되지 않은 예외 발생: {str(e)}", exc_info=True)
        return func.HttpResponse(json.dumps({"error": f"서버 내부 오류: {str(e)}"}), status_code=500, mimetype="application/json")
