import requests
import time
import concurrent.futures
import logging
from typing import List, Dict

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

# 设置API端点和请求头
url = "http://0.0.0.0:8899/v2/models/ensemble/generate"
headers = {"Content-Type": "application/json"}

# 目标语言列表
target_langs = ["en"]

# 测试不同的并发数
concurrency_levels = [1, 10, 100]  # , 128, 200]  # , 256, 300]

base_text = "综合美国福克斯新闻及法新社2月3日报道，刚刚卸任的美国前总统乔·拜登已与美国知名的艺人经纪公司创新艺人经纪公司（Creative Artists Agency，简称CAA）签约。法新社报道称，“这位美国前总统正在探索离开白宫后的工作”。根据CAA2月3日在社交媒体上发布的声明，他们此前已在拜登任职副总统期间与其签约。在上次签约期间，出版了拜登的个人回忆录并进行了为期42天的巡演，在全美范围内售出超8.5万张门票，并完成多场演讲活动。乔·拜登作为民主党总统候选人于2020年总统选举中击败当时的共和党总统候选人特朗普，之后就任美国总统，并在今年1月20日正式卸任。在2024年美国总统选举中，共和党总统候选人特朗普则胜出，在今年1月20日正式接替拜登成为美国总统。此次拜登签约经纪公司，距离其正式卸任仅过去约两周。创新艺人经纪公司（CAA）是美国知名的经纪公司，客户包括电影明星、体育界和音乐界名人，并且与不少政客也有往来。有记录显示，美国前总统奥巴马及其夫人均与该公司有合作，美国前国务卿希拉里也与该公司有过签约。"


def translate_batch(batch: str, lang: str) -> Dict:
    """
    翻译单个文本

    Args:
        batch: 待翻译文本
        lang: 目标语言

    Returns:
        包含翻译结果和统计信息的字典
    """
    data = {
        "text_input": f"<2{lang}> {batch}",
        "max_tokens": 511,
        "bad_words": "",
        "stop_words": "",
        "end_id": 2,
        "pad_id": 1,
    }

    max_retries = 10
    retry_count = 0

    while retry_count < max_retries:
        start_time = time.time()
        try:
            response = requests.post(url, json=data, headers=headers, timeout=30)

            result = {
                "time": time.time() - start_time,
                "status": response.status_code,
            }

            if response.status_code == 200:
                result["output"] = response.json()["text_output"]
                return result
            else:
                retry_count += 1
                if retry_count == max_retries:
                    result["error"] = f"HTTP错误: {response.status_code}"
                    logging.error(
                        f"翻译失败: {response.status_code}, 已重试{retry_count}次"
                    )
                    return result
                logging.warning(
                    f"翻译失败: {response.status_code}, 正在进行第{retry_count}次重试"
                )
                time.sleep(0.1)  # 重试前等待1秒

        except requests.Timeout:
            retry_count += 1
            if retry_count == max_retries:
                result = {
                    "time": time.time() - start_time,
                    "status": -1,
                    "error": "请求超时",
                }
                logging.error(f"请求超时, 已重试{retry_count}次")
                return result
            logging.warning(f"请求超时, 正在进行第{retry_count}次重试")
            time.sleep(1)

        except Exception as e:
            retry_count += 1
            if retry_count == max_retries:
                result = {
                    "time": time.time() - start_time,
                    "status": -1,
                    "error": str(e),
                }
                logging.error(f"发生错误: {str(e)}, 已重试{retry_count}次")
                return result
            logging.warning(f"发生错误: {str(e)}, 正在进行第{retry_count}次重试")
            time.sleep(1)


def run_translation_test(text: List[str], concurrency: int) -> tuple:
    """
    运行翻译测试

    Args:
        text: 待翻译文本列表
        concurrency: 并发数

    Returns:
        tuple: (平均耗时, 总成功数, 总失败数, 示例译文列表)
    """
    total_times = []
    total_success = 0
    total_errors = 0
    sample_translations = []

    for test_round in range(3):
        logging.info(f"开始第 {test_round + 1} 轮测试 (并发数: {concurrency})")
        total_start_time = time.time()
        success_count = 0
        error_count = 0

        for lang in target_langs:
            total_time = 0
            results = []

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=concurrency
            ) as executor:
                futures = [executor.submit(translate_batch, t, lang) for t in text]

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    results.append(result)
                    if result["status"] == 200:
                        success_count += 1
                        total_time += result["time"]
                        # 均匀采样译文示例
                        if (
                            len(sample_translations) < 3
                            and success_count % max(1, concurrency // 3) == 0
                        ):
                            sample_translations.append(result["output"])
                    else:
                        error_count += 1

        total_time = time.time() - total_start_time
        total_times.append(total_time)
        total_success += success_count
        total_errors += error_count

    avg_time = sum(total_times) / len(total_times)
    return avg_time, total_success, total_errors, sample_translations


if __name__ == "__main__":
    results = []
    for concurrency in concurrency_levels:
        text = [base_text] * concurrency
        avg_time, success_count, error_count, samples = run_translation_test(
            text, concurrency
        )
        results.append((concurrency, avg_time, success_count, error_count, samples))

    print("\n并发测试结果:")
    print("-" * 120)
    print(
        f"{'并发数':^10} | {'平均耗时(秒)':^15} | {'成功数':^12} | {'失败数':^12} | {'译文示例':^50}"
    )
    print("-" * 120)

    for concurrency, avg_time, success_count, error_count, samples in results:
        sample_text = samples[0][:50] + "..." if samples else "无示例"
        print(
            f"{concurrency:^10} | {avg_time:^15.2f} | {success_count:^12} | {error_count:^12} | {sample_text:^50}"
        )
        if samples:
            for i, sample in enumerate(samples[1:], 1):
                print(
                    f"{' ':^10} | {' ':^15} | {' ':^12} | {' ':^12} | {(sample[:50] + '...'):^50}"
                )
    print("-" * 120)
