# 파일 이름: function_app.py
# 설명: STT 완료 후, Azure AI Language 서비스를 호출하여
# 전체 내용 요약 및 문장별 핵심 구절을 추출하는 기능이 추가되었습니다.

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

# Language 서비스를 사용하기 위한 새로운 import
from azure.core.credentials import AzureKeyCredential
from azure.ai.textanalytics import TextAnalyticsClient

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

@app.route(route="UploadAndTranscribe", methods=["POST"])
def upload_and_transcribe(req: func.HttpRequest) -> func.HttpResponse:
    logging.info('Python HTTP trigger function: "UploadAndTranscribe"가 요청을 받았습니다.')

    # --- 환경 변수 로드 (Speech 및 Language 서비스) ---
    try:
        # 기존 Speech 서비스 환경 변수
        speech_key = os.environ['SPEECH_KEY']
        speech_region = os.environ['SPEECH_REGION']
        conn_str = os.environ['STORAGE_CONNECTION_STRING']
        
        # ★★★ 새로 추가된 Language 서비스 환경 변수 ★★★
        language_key = os.environ['LANGUAGE_KEY']
        language_endpoint = os.environ['LANGUAGE_ENDPOINT']

    except KeyError as e:
        logging.error(f"환경 변수 설정 오류: {e}를 찾을 수 없습니다. Azure Function 설정을 확인하세요.")
        return func.HttpResponse(json.dumps({"error": f"설정 오류: {e} 환경 변수가 누락되었습니다."}), status_code=500, mimetype="application/json")

    # --- FFmpeg 설정 ---
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        ffmpeg_path = os.path.join(script_dir, 'bin', 'ffmpeg.exe')
        ffprobe_path = os.path.join(script_dir, 'bin', 'ffprobe.exe')
        if not os.path.exists(ffmpeg_path) or not os.path.exists(ffprobe_path):
            raise FileNotFoundError("api/bin 폴더에 ffmpeg.exe 또는 ffprobe.exe 파일이 없습니다.")
        AudioSegment.converter = ffmpeg_path
        AudioSegment.ffprobe = ffprobe_path
    except Exception as ffmpeg_e:
        return func.HttpResponse(json.dumps({"error": f"서버 환경 설정 오류 (FFmpeg): {str(ffmpeg_e)}"}), status_code=500, mimetype="application/json")

    # --- 1. 파일 업로드 및 오디오 변환 ---
    try:
        file = req.files.get('file')
        if not file: return func.HttpResponse(json.dumps({"error": "요청에 파일이 포함되지 않았습니다."}), status_code=400, mimetype="application/json")
        file_bytes = file.stream.read()
        
        audio = AudioSegment.from_file(io.BytesIO(file_bytes))
        audio = audio.set_frame_rate(16000).set_channels(1)
        wav_buffer = io.BytesIO()
        audio.export(wav_buffer, format="wav")
        wav_buffer.seek(0)
    except Exception as audio_e:
        return func.HttpResponse(json.dumps({"error": "오디오 파일을 처리할 수 없습니다. 파일이 손상되었거나 지원되지 않는 형식일 수 있습니다."}), status_code=400, mimetype="application/json")

    # --- 2. STT(음성 텍스트 변환) 수행 ---
    try:
        blob_service_client = BlobServiceClient.from_connection_string(conn_str)
        blob_name = f"{str(uuid.uuid4())}.wav"
        blob_client = blob_service_client.get_blob_client(container='audio-files', blob=blob_name)
        blob_client.upload_blob(wav_buffer, overwrite=True)

        sas_token = generate_blob_sas(account_name=blob_service_client.account_name, container_name='audio-files', blob_name=blob_name, account_key=blob_service_client.credential.account_key, permission=BlobSasPermissions(read=True), expiry=datetime.utcnow() + timedelta(hours=1))
        sas_url = f"{blob_client.url}?{sas_token}"
        
        stt_endpoint = f"https://{speech_region}.api.cognitive.microsoft.com/speechtotext/v3.2/transcriptions"
        headers = {'Ocp-Apim-Subscription-Key': speech_key, 'Content-Type': 'application/json'}
        body = {"contentUrls": [sas_url], "locale": "ko-KR", "displayName": "Summary-Enabled Transcription", "properties": {"wordLevelTimestampsEnabled": True, "diarizationEnabled": True}}

        response = requests.post(stt_endpoint, headers=headers, json=body)
        if response.status_code != 201: raise Exception(f"Speech API 오류: {response.text}")
        
        transcription_url = response.headers['Location']
        transcription_result = poll_for_stt_result(transcription_url, headers)
        if not transcription_result: raise Exception("STT 작업 시간 초과 또는 실패")

    except Exception as stt_e:
        logging.error(f"STT 처리 중 오류: {stt_e}", exc_info=True)
        return func.HttpResponse(json.dumps({"error": str(stt_e)}), status_code=500, mimetype="application/json")

    # --- 3. Language 서비스로 요약 및 핵심 구절 추출 ---
    try:
        text_analytics_client = TextAnalyticsClient(endpoint=language_endpoint, credential=AzureKeyCredential(language_key))
        
        phrases = transcription_result.get("recognizedPhrases", [])
        full_text_for_summary = " ".join([p.get("nBest", [{}])[0].get("display", "") for p in phrases])

        # 전체 문서 요약
        summary = ""
        if full_text_for_summary.strip():
            summary_result = text_analytics_client.begin_extract_summary(documents=[full_text_for_summary]).result()
            for result in summary_result:
                if not result.is_error:
                    summary = " ".join([sentence.text for sentence in result.sentences])
                    break # 첫 번째 문서의 요약만 사용
        
        # 각 문장의 핵심 구절 추출
        for phrase in phrases:
            display_text = phrase.get("nBest", [{}])[0].get("display", "")
            if display_text.strip():
                key_phrases_result = text_analytics_client.extract_key_phrases(documents=[display_text])
                phrase["key_phrases"] = key_phrases_result[0].key_phrases if not key_phrases_result[0].is_error else []
            else:
                phrase["key_phrases"] = []

        # 최종 응답 데이터 구성
        final_response = {
            "summary": summary,
            "recognizedPhrases": phrases
        }
        return func.HttpResponse(json.dumps(final_response, ensure_ascii=False), status_code=200, mimetype="application/json; charset=utf-8")

    except Exception as lang_e:
        logging.error(f"Language 서비스 처리 중 오류: {lang_e}", exc_info=True)
        # Language 서비스에 문제가 생겨도 STT 결과는 반환하도록 처리
        return func.HttpResponse(json.dumps(transcription_result, ensure_ascii=False), status_code=200, mimetype="application/json; charset=utf-8")


def poll_for_stt_result(url: str, headers: dict) -> dict:
    """STT 작업이 완료될 때까지 주기적으로 상태를 확인하고 결과를 반환합니다."""
    poll_count = 0
    while poll_count < 30:
        time.sleep(10)
        res = requests.get(url, headers=headers)
        data = res.json()
        status = data.get('status')
        logging.info(f"현재 변환 상태: {status}")
        if status == 'Succeeded':
            files_url = data['links']['files']
            files_res = requests.get(files_url, headers=headers)
            content_url = files_res.json()['values'][0]['links']['contentUrl']
            content_res = requests.get(content_url)
            return content_res.json()
        elif status == 'Failed':
            return None
        poll_count += 1
    return None
