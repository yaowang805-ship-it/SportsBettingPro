import requests
import os
from dotenv import load_dotenv
load_dotenv()
WEBHOOK = os.getenv("DINGTALK_WEBHOOK")
def alert(title, text):
    if not WEBHOOK: return
    data = {"msgtype":"markdown","markdown":{"title":title,"text":text}}
    try: requests.post(WEBHOOK, json=data, timeout=5)
    except Exception: pass
