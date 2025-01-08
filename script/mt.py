import requests
import time
import concurrent.futures
import math

# 设置API端点和请求头
url = "http://0.0.0.0:8899/v2/models/ensemble/generate"
headers = {"Content-Type": "application/json"}

# 目标语言列表
# target_langs = ["en", "pt", "ja", "zh", "ko", "ru", "fr", "de"]
target_langs = ["en"]

# 生成44个元素的文本数组
text = [
    "各方确认及同意本协议的存在、本协议中的安排及内容，以及彼此就本次投资以及本协议的履行而交换或获得的任何口头或书面资料均属保密信息。各方应当对所有该等保密信息予以保密，在未取得另一方书面同意前，不得向任何第三者披露任何有关保密信息，惟下列情况除外：（a）该保密信息已经进入公共领域（惟并非由接受保密信息之一方擅自向公众披露）；（b）适用法律法规或行政或司法部门要求所需披露之资料；"
] * 44


def translate_batch(batch, lang):
    data = {
        "text_input": f"<2{lang}> {batch}",
        "max_tokens": 511,
        "bad_words": "",
        "stop_words": "",
        "end_id": 2,
        "pad_id": 1,
    }

    start_time = time.time()
    response = requests.post(url, json=data, headers=headers)
    end_time = time.time()

    result = {
        "time": end_time - start_time,
        "status": response.status_code,
    }

    if response.status_code == 200:
        result["output"] = response.json()["text_output"]

    return result


print("\n原文个数:", len(text))
print("-" * 40)
# 使用线程池进行并发翻译
total_start_time = time.time()

for lang in target_langs:
    results = []

    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [executor.submit(translate_batch, t, lang) for t in text]

        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            results.append(result)

total_end_time = time.time()
print(f"\n总耗时: {total_end_time - total_start_time:.2f} 秒")
