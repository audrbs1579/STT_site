# 파일 이름: function_app.py
# 설명: 사용자가 업로드한 오디오 파일(MP3 등)을 WAV 형식으로 변환하고,
# Azure Blob Storage에 업로드한 뒤, Azure AI Speech 서비스를 통해
# 텍스트로 변환하는 전체 과정을 처리하는 Azure Function 코드입니다.
# torchaudio 대신 pydub을 사용하여 안정성을 높였습니다.

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
from pydub import AudioSegment # torchaudio 대신 pydub 사용

# v2 프로그래밍 모델에 따라 FunctionApp 인스턴스를 생성합니다.
# http_auth_level=func.AuthLevel.ANONYMOUS: 인증 없이 누구나 호출할 수 있도록 설정합니다.
app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="UploadAndTranscribe", methods=["POST"])
def upload_and_transcribe(req: func.HttpRequest) -> func.HttpResponse:
    """
    HTTP POST 요청을 받아 오디오 파일을 업로드하고 텍스트 변환을 수행하는 메인 함수
    """
    logging.info('Python HTTP trigger function: "UploadAndTranscribe"가 요청을 받았습니다.')

    try:
        # 1. 파일 업로드 확인
        # form-data에서 'file'이라는 이름의 파일을 가져옵니다.
        file = req.files.get('file')
        if not file:
            logging.warning("업로드된 파일이 없습니다.")
            return func.HttpResponse(
                json.dumps({"error": "요청에 파일이 포함되지 않았습니다."}),
                status_code=400,
                mimetype="application/json"
            )

        filename = file.filename
        file_bytes = file.stream.read()
        logging.info(f"파일 수신 완료: {filename}, 크기: {len(file_bytes)} bytes")

        # 2. 오디오 변환 (pydub 사용)
        # 업로드된 오디오 파일을 메모리에서 pydub으로 로드합니다.
        logging.info("pydub을 사용하여 오디오 변환을 시작합니다...")
        try:
            audio = AudioSegment.from_file(io.BytesIO(file_bytes))
            logging.info("오디오 파일을 성공적으로 로드했습니다.")

            # Azure AI Speech가 요구하는 형식(16kHz, 모노)으로 변환
            audio = audio.set_frame_rate(16000)
            audio = audio.set_channels(1)
            logging.info("오디오를 16kHz, 모노 채널로 변환했습니다.")

            # 변환된 오디오를 WAV 형식으로 메모리 버퍼에 저장
            wav_buffer = io.BytesIO()
            audio.export(wav_buffer, format="wav")
            wav_buffer.seek(0)
            logging.info("WAV 형식으로 메모리 내 변환을 완료했습니다.")

        except Exception as audio_e:
            logging.error(f"오디오 변환 중 심각한 오류 발생: {str(audio_e)}")
            return func.HttpResponse(
                json.dumps({"error": f"오디오 파일 처리 오류: {str(audio_e)}"}),
                status_code=400,
                mimetype="application/json"
            )

        # 3. Blob Storage에 변환된 WAV 파일 업로드
        conn_str = os.environ['STORAGE_CONNECTION_STRING']
        container_name = 'audio-files'
        
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        
        # 고유한 파일 이름 생성 (UUID 사용)
        blob_name = f"{str(uuid.uuid4())}.wav"
        
        blob_client = blob_service_client.get_blob_client(container=container_name, blob=blob_name)
        
        logging.info(f"'{blob_name}' 이름으로 Blob Storage에 업로드를 시작합니다...")
        blob_client.upload_blob(wav_buffer, overwrite=True)
        logging.info("Blob Storage에 업로드 완료.")

        # 4. Speech-to-Text를 위한 SAS URL 생성
        sas_token = generate_blob_sas(
            account_name=blob_service_client.account_name,
            container_name=container_name,
            blob_name=blob_name,
            account_key=blob_service_client.credential.account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.utcnow() + timedelta(hours=1)
        )
        sas_url = f"{blob_client.url}?{sas_token}"
        logging.info("파일 접근을 위한 SAS URL 생성 완료.")

        # 5. Azure AI Speech 배치(Batch) 변환 API 호출
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
            "displayName": "My Transcription Task",
            "properties": {
                "wordLevelTimestampsEnabled": True,
                "diarizationEnabled": True # 화자 분리 기능 활성화 (필요 시)
            }
        }

        logging.info("Azure AI Speech API에 텍스트 변환 요청을 보냅니다...")
        response = requests.post(endpoint, headers=headers, json=body)
        
        if response.status_code != 201:
            error_msg = response.text
            logging.error(f"Speech API 요청 실패. 상태 코드: {response.status_code}, 메시지: {error_msg}")
            return func.HttpResponse(json.dumps({"error": f"Speech API 오류: {error_msg}"}), status_code=500, mimetype="application/json")
        
        transcription_url = response.headers['Location']
        logging.info(f"텍스트 변환 작업이 생성되었습니다. 상태 확인 URL: {transcription_url}")

        # 6. 변환 결과 폴링(Polling)
        # 작업이 완료될 때까지 주기적으로 상태를 확인합니다.
        poll_count = 0
        while poll_count < 30: # 최대 5분 (30 * 10초) 동안 확인
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
                
                logging.info("텍스트 변환 성공!")
                # 전체 텍스트만 추출하고 싶을 경우
                # full_text = " ".join([item['display'] for item in transcription_result['recognizedPhrases']])
                
                return func.HttpResponse(json.dumps(transcription_result), status_code=200, mimetype="application/json")
            
            elif status == 'Failed':
                error_info = status_data.get('properties', {}).get('error', {})
                error_msg = error_info.get('message', '알 수 없는 변환 실패')
                logging.error(f"텍스트 변환 실패: {error_msg}")
                return func.HttpResponse(json.dumps({"error": error_msg}), status_code=500, mimetype="application/json")
            
            poll_count += 1
        
        logging.warning("텍스트 변환 작업 시간 초과.")
        return func.HttpResponse(json.dumps({"error": "Transcription timed out after 5 minutes."}), status_code=500, mimetype="application/json")

    except Exception as e:
        # 예상치 못한 모든 오류를 여기서 처리합니다.
        logging.error(f"처리되지 않은 예외 발생: {str(e)}", exc_info=True)
        return func.HttpResponse(json.dumps({"error": f"서버 내부 오류: {str(e)}"}), status_code=500, mimetype="application/json")

