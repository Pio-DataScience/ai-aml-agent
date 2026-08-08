import httpx
import time

url_post = "http://127.0.0.1:8005/chat/no_project/test_chat_id_async_001/user_dev_01"
url_status = "http://127.0.0.1:8005/chat/no_project/test_chat_id_async_001/user_dev_01/status"

payload = {
    "messages": [
        {
            "role": "user",
            "content": "ايداع واحد بقيمة عالية فوق ال 10 اللف في اليوم"
        }
    ],
    "metadata": {
        "user_id": "user_dev_01",
        "chat_id": "test_chat_id_async_001",
        "project_id": "no_project"
    }
}

print("Sending POST request to start async scenario run...")
resp = httpx.post(url_post, json=payload, timeout=10.0)
print("Response code:", resp.status_code)
print("Response JSON:", resp.json())

task_id = resp.json().get("task_id")
status = resp.json().get("status")

if resp.status_code == 202 and status == "RUNNING":
    print("Success: Endpoint returned 202 and RUNNING.")
    
    # Poll status
    for i in range(15):
        time.sleep(2)
        status_resp = httpx.get(url_status)
        print(f"Poll {i+1} status:")
        print(status_resp.json())
        if status_resp.json().get("status") in ("COMPLETED", "FAILED"):
            break
else:
    print("Failure: Response was not as expected.")
